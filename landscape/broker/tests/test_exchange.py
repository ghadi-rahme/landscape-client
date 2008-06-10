import md5

from landscape import API
from landscape.lib.persist import Persist
from landscape.schema import Message, Int
from landscape.broker.exchange import get_accepted_types_diff, MessageExchange
from landscape.broker.transport import FakeTransport
from landscape.broker.store import MessageStore
from landscape.tests.helpers import LandscapeTest, ExchangeHelper


class MessageExchangeTest(LandscapeTest):

    helpers = [ExchangeHelper]

    def setUp(self):
        super(MessageExchangeTest, self).setUp()
        self.mstore.add_schema(Message("empty", {}))
        self.mstore.add_schema(Message("data", {"data": Int()}))
        self.mstore.add_schema(Message("holdme", {}))

    def wait_for_exchange(self, urgent=False, factor=1, delta=0):
        if urgent:
            seconds = self.broker_service.config.urgent_exchange_interval
        else:
            seconds = self.broker_service.config.exchange_interval
        self.reactor.advance(seconds * factor + delta)

    def test_resynchronize_causes_urgent_exchange(self):
        """
        A 'resynchronize-clients' messages causes an urgent exchange
        to be scheduled.
        """
        self.assertFalse(self.broker_service.exchanger.is_urgent())
        self.reactor.fire("resynchronize-clients")
        self.assertTrue(self.broker_service.exchanger.is_urgent())

    def test_send(self):
        """
        The send method should cause a message to show up in the next exchange.
        """
        self.mstore.set_accepted_types(["empty"])
        self.exchanger.send({"type": "empty"})
        self.exchanger.exchange()
        self.assertEquals(len(self.transport.payloads), 1)
        messages = self.transport.payloads[0]["messages"]
        self.assertEquals(messages, [{"type": "empty",
                                      "timestamp": 0,
                                      "api": API}])

    def test_send_urgent(self):
        """
        Sending a message with the urgent flag should schedule an
        urgent exchange.
        """
        self.mstore.set_accepted_types(["empty"])
        self.exchanger.send({"type": "empty"}, urgent=True)
        self.wait_for_exchange(urgent=True)
        self.assertEquals(len(self.transport.payloads), 1)
        self.assertMessages(self.transport.payloads[0]["messages"],
                            [{"type": "empty"}])

    def test_send_urgent_wont_reschedule(self):
        """
        If an urgent exchange is already scheduled, adding another
        urgent message shouldn't reschedule the exchange forward.
        """
        self.mstore.set_accepted_types(["empty"])
        self.exchanger.send({"type": "empty"}, urgent=True)
        self.wait_for_exchange(urgent=True, factor=0.5)
        self.exchanger.send({"type": "empty"}, urgent=True)
        self.wait_for_exchange(urgent=True, factor=0.5)
        self.assertEquals(len(self.transport.payloads), 1)
        self.assertMessages(self.transport.payloads[0]["messages"],
                            [{"type": "empty"}, {"type": "empty"}])

    def test_send_returns_message_id(self):
        """
        The send method should return the message id, as returned by add().
        """
        self.mstore.set_accepted_types(["empty"])
        message_id = self.exchanger.send({"type": "empty"})
        self.assertTrue(self.mstore.is_pending(message_id))
        self.mstore.add_pending_offset(1)
        self.assertFalse(self.mstore.is_pending(message_id))

    def test_include_accepted_types(self):
        """
        Every payload from the client needs to specify an ID which
        represents the types that we think the server wants.
        """
        payload = self.exchanger.make_payload()
        self.assertTrue("accepted-types" in payload)
        self.assertEquals(payload["accepted-types"], md5.new("").digest())

    def test_set_accepted_types(self):
        """
        An incoming "accepted-types" message should set the accepted
        types.
        """
        self.reactor.fire("message",
                          {"type": "accepted-types", "types": ["foo"]})
        self.assertEquals(self.mstore.get_accepted_types(), ["foo"])

    def test_message_type_acceptance_changed_event(self):
        stash = []
        def callback(type, accepted):
            stash.append((type, accepted))
        self.reactor.call_on("message-type-acceptance-changed", callback)
        self.reactor.fire("message",
                          {"type": "accepted-types", "types": ["a", "b"]})
        self.reactor.fire("message",
                          {"type": "accepted-types", "types": ["b", "c"]})
        self.assertEquals(stash, [("a", True), ("b", True),
                                  ("a", False), ("c", True)])

    def test_accepted_types_roundtrip(self):
        """
        Telling the client to set the accepted types with a message
        should affect its future payloads.
        """
        self.reactor.fire("message",
                          {"type": "accepted-types", "types": ["ack", "bar"]})
        payload = self.exchanger.make_payload()
        self.assertTrue("accepted-types" in payload)
        self.assertEquals(payload["accepted-types"],
                          md5.new("ack;bar").digest())

    def test_accepted_types_causes_urgent_if_held_messages_exist(self):
        """
        If an accepted-types message makes available a type for which we
        have a held message, an urgent exchange should occur.
        """
        self.exchanger.send({"type": "holdme"})
        self.assertEquals(self.mstore.get_pending_messages(), [])
        self.reactor.fire("message",
                          {"type": "accepted-types", "types": ["holdme"]})
        self.wait_for_exchange(urgent=True)
        self.assertEquals(len(self.transport.payloads), 1)
        self.assertMessages(self.transport.payloads[0]["messages"],
                            [{"type": "holdme"}])

    def test_accepted_types_no_urgent_without_held(self):
        """
        If an accepted-types message does *not* "unhold" any exist messages,
        then no urgent exchange should occur.
        """
        self.exchanger.send({"type": "holdme"})
        self.assertEquals(self.transport.payloads, [])
        self.reactor.fire("message",
                          {"type": "accepted-types", "types": ["irrelevant"]})
        self.assertEquals(len(self.transport.payloads), 0)

    def test_messages_from_server(self):
        """
        The client should process messages in the response from the server. For
        every message, a reactor event 'message' should be fired with the
        message passed as an argument.
        """
        server_message = [{"type": "foobar", "value": "hi there"}]
        self.transport.responses.append(server_message)

        responses = []
        def handler(message):
            responses.append(message)

        id = self.reactor.call_on("message", handler)
        self.exchanger.exchange()
        self.assertEquals(responses, server_message)

    def test_sequence_is_committed_immediately(self):
        """
        The MessageStore should be committed by the MessageExchange as soon as
        possible after setting the pending offset and sequence.
        """
        self.mstore.set_accepted_types(["empty"])

        # We'll check that the message store has been saved by the time a
        # message handler gets called.
        self.transport.responses.append([{"type": "inbound"}])
        self.exchanger.send({"type": "empty"})

        handled = []
        def handler(message):
            service = self.broker_service
            persist = Persist(filename=service.persist_filename)
            store = MessageStore(persist, service.config.message_store_path)
            self.assertEquals(store.get_pending_offset(), 1)
            self.assertEquals(store.get_sequence(), 1)
            handled.append(True)

        self.reactor.call_on("message", handler)
        self.exchanger.exchange()
        self.assertEquals(handled, [True], self.logfile.getvalue())

    def test_messages_from_server_commit(self):
        """
        The Exchange should commit the message store after processing each
        message.
        """
        self.transport.responses.append([{"type": "inbound"}]*3)
        handled = []
        self.message_counter = 0

        def handler(message):
            service = self.broker_service
            persist = Persist(filename=service.persist_filename)
            store = MessageStore(persist, service.config.message_store_path)
            self.assertEquals(store.get_server_sequence(), self.message_counter)
            self.message_counter += 1
            handled.append(True)

        self.reactor.call_on("message", handler)
        self.exchanger.exchange()
        self.assertEquals(handled, [True]*3, self.logfile.getvalue())

    def test_messages_from_server_causing_urgent_exchanges(self):
        """
        If a message from the server causes an urgent message to be
        queued, an urgent exchange should happen again after the
        running exchange.
        """
        self.transport.responses.append([{"type": "foobar"}])
        self.mstore.set_accepted_types(["empty"])

        def handler(message):
            self.exchanger.send({"type": "empty"}, urgent=True)

        self.reactor.call_on("message", handler)

        self.exchanger.exchange()

        self.assertEquals(len(self.transport.payloads), 1)

        self.wait_for_exchange(urgent=True)

        self.assertEquals(len(self.transport.payloads), 2)
        self.assertMessages(self.transport.payloads[1]["messages"],
                            [{"type": "empty"}])

    def test_server_expects_older_messages(self):
        """
        If the server expects an old message, the exchanger should be
        marked as urgent.
        """
        self.mstore.set_accepted_types(["data"])
        self.mstore.add({"type": "data", "data": 0})
        self.mstore.add({"type": "data", "data": 1})
        self.exchanger.exchange()
        self.assertEquals(self.mstore.get_sequence(), 2)

        self.mstore.add({"type": "data", "data": 2})
        self.mstore.add({"type": "data", "data": 3})
        # next one, server will respond with 1!
        def desynched_send_data(payload, computer_id=None, message_api=None):
            self.transport.next_expected_sequence = 1
            return {"next-expected-sequence": 1}

        self.transport.exchange = desynched_send_data
        self.exchanger.exchange()
        self.assertEquals(self.mstore.get_sequence(), 1)
        del self.transport.exchange

        exchanged = []
        def exchange_callback():
            exchanged.append(True)

        self.reactor.call_on("exchange-done", exchange_callback)
        self.wait_for_exchange(urgent=True)
        self.assertEquals(exchanged, [True])

        payload = self.transport.payloads[-1]
        self.assertMessages(payload["messages"],
                            [{"type": "data", "data": 1},
                             {"type": "data", "data": 2},
                             {"type": "data", "data": 3}])
        self.assertEquals(payload["sequence"], 1)
        self.assertEquals(payload["next-expected-sequence"], 0)

    def test_start_with_urgent_exchange(self):
        """
        Immediately after registration, an urgent exchange should be scheduled.
        """
        transport = FakeTransport()
        exchanger = MessageExchange(self.reactor, self.mstore, transport,
                                    self.identity)
        exchanger.start()
        self.wait_for_exchange(urgent=True)
        self.assertEquals(len(transport.payloads), 1)

    def test_reschedule_after_exchange(self):
        """
        Under normal operation, after an exchange has finished another
        exchange should be scheduled for after the normal delay.
        """
        self.exchanger.schedule_exchange(urgent=True)

        self.wait_for_exchange(urgent=True)
        self.assertEquals(len(self.transport.payloads), 1)

        self.wait_for_exchange()
        self.assertEquals(len(self.transport.payloads), 2)

        self.wait_for_exchange()
        self.assertEquals(len(self.transport.payloads), 3)

    def test_leave_urgent_exchange_mode_after_exchange(self):
        """
        After an urgent exchange, assuming no messages are left to be
        exchanged, urgent exchange should not remain scheduled.
        """
        self.mstore.set_accepted_types(["empty"])
        self.exchanger.send({"type": "empty"}, urgent=True)
        self.wait_for_exchange(urgent=True)
        self.assertEquals(len(self.transport.payloads), 1)
        self.wait_for_exchange(urgent=True)
        self.assertEquals(len(self.transport.payloads), 1) # no change

    def test_ancient_causes_resynchronize(self):
        """
        If the server asks for messages that we no longer have, the message
        exchange plugin should send a message to the server indicating that a
        resynchronization is occuring and then fire a "resynchronize-clients"
        reactor message, so that plugins can generate new data -- if the server
        got out of synch with the client, then we're best off synchronizing
        everything back to it.
        """
        self.mstore.set_accepted_types(["empty", "data", "resynchronize"])
        # Do three generations of messages, so we "lose" the 0th message
        for i in range(3):
            self.mstore.add({"type": "empty"})
            self.exchanger.exchange()
        # the server loses some data
        self.transport.next_expected_sequence = 0

        def resynchronize():
            # We'll add a message to the message store here, since this is what
            # is commonly done in a resynchronize callback. This message added
            # should come AFTER the "resynchronize" message that is generated
            # by the exchange code itself.
            self.mstore.add({"type": "data", "data": 999})
        self.reactor.call_on("resynchronize-clients", resynchronize)

        # This exchange call will notice the server is asking for an old
        # message and fire the event:
        self.exchanger.exchange()
        self.assertMessages(self.mstore.get_pending_messages(),
                            [{"type": "empty"},
                             {"type": "resynchronize"},
                             {"type": "data", "data": 999}])

    def test_resynchronize_msg_causes_resynchronize_response_then_event(self):
        """
        If a message of type 'resynchronize' is received from the
        server, the exchanger should *first* send a 'resynchronize'
        message back to the server and *then* fire a 'resynchronize-clients'
        event.
        """
        self.mstore.set_accepted_types(["empty", "resynchronize"])
        def resynchronized():
            self.mstore.add({"type": "empty"})
        self.reactor.call_on("resynchronize-clients", resynchronized)

        self.transport.responses.append([{"type": "resynchronize",
                                          "operation-id": 123}])
        self.exchanger.exchange()
        self.assertMessages(self.mstore.get_pending_messages(),
                            [{"type": "resynchronize",
                              "operation-id": 123},
                             {"type": "empty"}])

    def test_no_urgency_when_server_expects_current_message(self):
        """
        When the message the server expects is the same as the first
        pending message sequence, the client should not go into urgent
        exchange mode.

        This means the server handler is likely blowing up and the client and
        the server are in a busy loop constantly asking for the same message,
        breaking, setting urgent exchange mode, sending the same message and
        then breaking in a fast loop.  In this case, urgent exchange mode
        should not be set. (bug #138135)
        """
        # We set the server sequence to some non-0 value to ensure that the
        # server and client sequences aren't the same to ensure the code is
        # looking at the correct sequence number. :(
        self.mstore.set_server_sequence(3300)
        self.mstore.set_accepted_types(["data"])
        self.mstore.add({"type": "data", "data": 0})

        def desynched_send_data(payload, computer_id=None, message_api=None):
            self.transport.next_expected_sequence = 0
            return {"next-expected-sequence": 0}

        self.transport.exchange = desynched_send_data
        self.exchanger.exchange()

        self.assertEquals(self.mstore.get_sequence(), 0)
        del self.transport.exchange

        exchanged = []
        def exchange_callback():
            exchanged.append(True)

        self.reactor.call_on("exchange-done", exchange_callback)
        self.wait_for_exchange(urgent=True)
        self.assertEquals(exchanged, [])
        self.wait_for_exchange()
        self.assertEquals(exchanged, [True])

    def test_old_sequence_id_does_not_cause_resynchronize(self):
        resynchronized = []
        self.reactor.call_on("resynchronize",
                             lambda: resynchronized.append(True))

        self.mstore.set_accepted_types(["empty"])
        self.mstore.add({"type": "empty"})
        self.exchanger.exchange()
        # the server loses some data, but not too much
        self.transport.next_expected_sequence = 0

        self.exchanger.exchange()
        self.assertEquals(resynchronized, [])

    def test_per_api_payloads(self):
        """
        When sending messages to the server, the exchanger should split
        messages with different APIs in different payloads, and deliver
        them to the right API on the server.
        """
        types = ["a", "b", "c", "d", "e", "f"]
        self.mstore.set_accepted_types(types)
        for t in types:
            self.mstore.add_schema(Message(t, {}))

        self.exchanger.exchange()

        # No messages queued yet.  Server API should default to
        # the client API.
        payload = self.transport.payloads[-1]
        self.assertMessages(payload["messages"], [])
        self.assertEquals(payload.get("client-api"), API)
        self.assertEquals(payload.get("server-api"), API)
        self.assertEquals(self.transport.message_api, API)

        self.mstore.add({"type": "a", "api": "1.0"})
        self.mstore.add({"type": "b", "api": "1.0"})
        self.mstore.add({"type": "c", "api": "1.1"})
        self.mstore.add({"type": "d", "api": "1.1"})

        # Simulate an old 2.0 client, which has no API on messages.
        self.mstore.add({"type": "e", "api": None})
        self.mstore.add({"type": "f", "api": None})

        self.exchanger.exchange()

        payload = self.transport.payloads[-1]
        self.assertMessages(payload["messages"],
                            [{"type": "a", "api": "1.0"},
                             {"type": "b", "api": "1.0"}])
        self.assertEquals(payload.get("client-api"), API)
        self.assertEquals(payload.get("server-api"), "1.0")
        self.assertEquals(self.transport.message_api, "1.0")

        self.exchanger.exchange()

        payload = self.transport.payloads[-1]
        self.assertMessages(payload["messages"],
                            [{"type": "c", "api": "1.1"},
                             {"type": "d", "api": "1.1"}])
        self.assertEquals(payload.get("client-api"), API)
        self.assertEquals(payload.get("server-api"), "1.1")
        self.assertEquals(self.transport.message_api, "1.1")

        self.exchanger.exchange()

        payload = self.transport.payloads[-1]
        self.assertMessages(payload["messages"],
                            [{"type": "e", "api": None},
                             {"type": "f", "api": None}])
        self.assertEquals(payload.get("client-api"), API)
        self.assertEquals(payload.get("server-api"), "2.0")
        self.assertEquals(self.transport.message_api, "2.0")


    def test_include_total_messages_none(self):
        """
        The payload includes the total number of messages that the client has
        pending for the server.
        """
        self.mstore.set_accepted_types(["empty"])
        self.exchanger.exchange()
        self.assertEquals(self.transport.payloads[0]["total-messages"], 0)

    def test_include_total_messages_some(self):
        """
        If there are no more messages than those that are sent in the exchange,
        the total-messages is equivalent to the number of messages sent.
        """
        self.mstore.set_accepted_types(["empty"])
        self.mstore.add({"type": "empty"})
        self.exchanger.exchange()
        self.assertEquals(self.transport.payloads[0]["total-messages"], 1)

    def test_include_total_messages_more(self):
        """
        If there are more messages than those that are sent in the exchange,
        the total-messages is equivalent to the total number of messages
        pending.
        """
        exchanger = MessageExchange(self.reactor, self.mstore, self.transport,
                                    self.identity, max_messages=1)
        self.mstore.set_accepted_types(["empty"])
        self.mstore.add({"type": "empty"})
        self.mstore.add({"type": "empty"})
        exchanger.exchange()
        self.assertEquals(self.transport.payloads[0]["total-messages"], 2)


    def test_impending_exchange(self):
        """
        A reactor event is emitted shortly (10 seconds) before an exchange
        occurs.
        """
        self.exchanger.schedule_exchange()
        events = []
        self.reactor.call_on("impending-exchange", lambda: events.append(True))
        self.wait_for_exchange(delta=-11)
        self.assertEquals(events, [])
        self.reactor.advance(1)
        self.assertEquals(events, [True])

    def test_impending_exchange_on_urgent(self):
        """
        The C{impending-exchange} event is fired 10 seconds before urgent
        exchanges.
        """
        # We create our own MessageExchange because the one set up by the text
        # fixture has an urgent exchange interval of 10 seconds, which makes
        # testing this awkward.
        exchanger = MessageExchange(self.reactor, self.mstore, self.transport,
                                    self.identity, urgent_exchange_interval=20)
        exchanger.schedule_exchange(urgent=True)
        events = []
        self.reactor.call_on("impending-exchange", lambda: events.append(True))
        self.reactor.advance(9)
        self.assertEquals(events, [])
        self.reactor.advance(1)
        self.assertEquals(events, [True])

    def test_impending_exchange_gets_reschudeled_with_urgent_reschedule(self):
        """
        When an urgent exchange is scheduled after a regular exchange was
        scheduled but before it executed, the old C{impending-exchange} event
        should be cancelled and a new one should be scheduled for 10 seconds
        before the new urgent exchange.
        """
        exchanger = MessageExchange(self.reactor, self.mstore, self.transport,
                                    self.identity, urgent_exchange_interval=20)
        events = []
        self.reactor.call_on("impending-exchange", lambda: events.append(True))
        # This call will:
        # * schedule the exchange for an hour from now
        # * schedule impending-exchange to be fired an hour - 10 seconds from
        #   now
        exchanger.schedule_exchange()
        # And this call will:
        # * hopefully cancel those previous calls
        # * schedule an exchange for 20 seconds from now
        # * schedule impending-exchange to be fired in 10 seconds
        exchanger.schedule_exchange(urgent=True)
        self.reactor.advance(10)
        self.assertEquals(events, [True])
        self.reactor.advance(10)
        self.assertEquals(len(self.transport.payloads), 1)
        # Now the urgent exchange should be fired, which should automatically
        # schedule a regular exchange.
        # Let's make sure that that *original* impending-exchange event has
        # been cancelled:
        self.reactor.advance(60 * 60 # time till exchange
                             - 10 # time till notification
                             - 20 # time that we've already advanced
                             )
        self.assertEquals(events, [True])
        # Ok, so no new events means that the original call was
        # cancelled. great.
        # Just a bit more sanity checking:
        self.reactor.advance(20)
        self.assertEquals(events, [True, True])
        self.reactor.advance(10)
        self.assertEquals(len(self.transport.payloads), 2)

    def test_pre_exchange_event(self):
        reactor_mock = self.mocker.patch(self.reactor)
        reactor_mock.fire("pre-exchange")
        self.mocker.replay()
        self.exchanger.exchange()

    def test_schedule_exchange(self):
        self.exchanger.schedule_exchange()
        self.wait_for_exchange(urgent=True)
        self.assertFalse(self.transport.payloads)
        self.wait_for_exchange()
        self.assertTrue(self.transport.payloads)

    def test_schedule_urgent_exchange(self):
        self.exchanger.schedule_exchange(urgent=True)
        self.wait_for_exchange(urgent=True)
        self.assertTrue(self.transport.payloads)

    def test_exchange_failed_fires_correctly(self):
        """
        Ensure that the exchange-failed event is fired if the
        exchanger raises an exception.
        """

        def failed_send_data(payload, computer_id=None, message_api=None):
            return None

        self.transport.exchange = failed_send_data

        exchanged = []
        def exchange_failed_callback():
            exchanged.append(True)

        self.reactor.call_on("exchange-failed", exchange_failed_callback)
        self.exchanger.exchange()
        self.assertEquals(exchanged, [True])

    def test_stop(self):
        self.exchanger.schedule_exchange()
        self.exchanger.stop()
        self.wait_for_exchange()
        self.assertFalse(self.transport.payloads)

    def test_stop_twice_doesnt_break(self):
        self.exchanger.schedule_exchange()
        self.exchanger.stop()
        self.exchanger.stop()
        self.wait_for_exchange()
        self.assertFalse(self.transport.payloads)

    def test_firing_pre_exit_will_stop_exchange(self):
        self.exchanger.schedule_exchange()
        self.reactor.fire("pre-exit")
        self.wait_for_exchange()
        self.assertFalse(self.transport.payloads)

    def test_default_exchange_intervals(self):
        self.assertEquals(self.exchanger.get_exchange_intervals(), (60, 900))

    def test_set_intervals(self):
        server_message = [{"type": "set-intervals",
                           "urgent-exchange": 1234, "exchange": 5678}]
        self.transport.responses.append(server_message)

        self.exchanger.exchange()

        self.assertEquals(self.exchanger.get_exchange_intervals(), (1234, 5678))

    def test_set_intervals_with_urgent_exchange_only(self):
        server_message = [{"type": "set-intervals", "urgent-exchange": 1234}]
        self.transport.responses.append(server_message)

        self.exchanger.exchange()

        self.assertEquals(self.exchanger.get_exchange_intervals(), (1234, 900))

        # Let's make sure it works.
        self.exchanger.schedule_exchange(urgent=True)
        self.reactor.advance(1233)
        self.assertEquals(len(self.transport.payloads), 1)
        self.reactor.advance(1)
        self.assertEquals(len(self.transport.payloads), 2)

    def test_set_intervals_with_exchange_only(self):
        server_message = [{"type": "set-intervals", "exchange": 5678}]
        self.transport.responses.append(server_message)

        self.exchanger.exchange()

        self.assertEquals(self.exchanger.get_exchange_intervals(), (60, 5678))

        # Let's make sure it works.
        self.reactor.advance(5677)
        self.assertEquals(len(self.transport.payloads), 1)
        self.reactor.advance(1)
        self.assertEquals(len(self.transport.payloads), 2)


class GetAcceptedTypesDiffTest(LandscapeTest):

    def test_diff_empty(self):
        self.assertEquals(get_accepted_types_diff([], []),
                          "")

    def test_diff_add(self):
        self.assertEquals(get_accepted_types_diff([], ["wubble"]),
                          "+wubble")

    def test_diff_remove(self):
        self.assertEquals(get_accepted_types_diff(["wubble"], []),
                          "-wubble")

    def test_diff_no_change(self):
        self.assertEquals(get_accepted_types_diff(["ooga"], ["ooga"]),
                          "ooga")

    def test_diff_complex(self):
        self.assertEquals(get_accepted_types_diff(["foo", "bar"],
                                                  ["foo", "ooga"]),
                          "+ooga foo -bar")


# XXX Let's make it the Exchanger's job to do accepted-types notification.

# class AcceptedTypesTest(LandscapeTest):
#     def test_set_accepted_types_event(self):
#         accepted = []
#         def got_accepted():
#             accepted.append(True)
#         self.reactor.call_on(("message-type-accepted", "fiznits"), got_accepted)

#         self.store.set_accepted_types(["fiznits"])
#         self.assertEquals(accepted, [True])

#     def test_newly_accepted_types_event(self):
#         """
#         When an accepted type is set by the server, fire an event that
#         notifies any listeners.  Existing acceptable types listeners
#         should not be notified, only newly accepted type listeners.
#         """
#         accepted = []
#         def got_accepted_fiznits():
#             accepted.append("fiznits")
#         def got_accepted_blobos():
#             accepted.append("blobos")

#         self.store.set_accepted_types(["blobos"])
#         self.reactor.call_on(("message-type-accepted", "fiznits"), got_accepted_fiznits)
#         self.reactor.call_on(("message-type-accepted", "blobos"), got_accepted_blobos)
#         self.store.set_accepted_types(["fiznits", "blobos"])
#         self.assertEquals(accepted, ["fiznits"])

#     def test_type_accepted_before_event(self):
#         """
#         When an accepted type is set by the server, fire an event that
#         notifies any listeners.  The event should be fired after the event
#         is accepted, not before.
#         """
#         accepted = []
#         def got_accepted():
#             accepted.append(self.store.get_accepted_types() == ["fiznits"])

#         self.reactor.call_on(("message-type-accepted", "fiznits"), got_accepted)
#         self.store.set_accepted_types(["fiznits"])
#         self.assertEquals(accepted, [True])

from twisted.protocols.amp import AMP
from twisted.internet.protocol import ServerFactory, ClientCreator
from twisted.internet.defer import succeed

from landscape.lib.amp import MethodCall


class BrokerServerProtocol(AMP):
    """
    Communication protocol between the broker server and its clients.
    """

    _broker_method_calls = ("ping",
                            "register_client",
                            "send_message",
                            "is_message_pending",
                            "stop_clients",
                            "reload_configuration",
                            "register",
                            "get_accepted_message_types",
                            "get_server_uuid",
                            "register_client_accepted_message_type",
                            "exit")

    @MethodCall.responder
    def _get_broker_method(self, name):
        if name in self._broker_method_calls:
            return getattr(self.factory.broker, name)


class BrokerServerProtocolFactory(ServerFactory):
    """A protocol factory for the L{BrokerProtocol}."""

    protocol = BrokerServerProtocol

    def __init__(self, broker):
        """
        @param: The L{BrokerServer} the connections will talk to.
        """
        self.broker = broker


class RemoteClient(object):
    """A connected client utilizing features provided by a L{BrokerServer}."""

    def __init__(self, name, protocol):
        """
        @param name: Name of the broker client.
        @param protocol: A L{BrokerServerProtocol} connection with the broker
            server.
        """
        self.name = name
        self._protocol = protocol

    def exit(self):
        """Placeholder to make tests pass, it will be replaced later."""
        return succeed(None)


class RemoteBroker(object):
    """A connected broker utilizing features provided by a L{BrokerServer}."""

    def __init__(self, config, reactor):
        """
        @param protocol: A L{BrokerServerProtocol} connection with a remote
            broker server.
        """
        self._config = config
        self._reactor = reactor
        self._protocol = None

    def connect(self):
        """Connect to the remote L{BrokerServer}."""

        def set_protocol(protocol):
            self._protocol = protocol

        connector = ClientCreator(self._reactor._reactor, AMP)
        socket = self._config.broker_socket_filename
        connected = connector.connectUNIX(socket)
        return connected.addCallback(set_protocol)

    def disconnect(self):
        """Disconnect from the remote L{BrokerServer}."""
        self._protocol.transport.loseConnection()
        self._protocol = None

    @MethodCall.sender
    def ping(self):
        """@see L{BrokerServer.ping}"""

    @MethodCall.sender
    def register_client(self, name, _protocol=""):
        """@see L{BrokerServer.register_client}"""

    @MethodCall.sender
    def send_message(self, message, urgent):
        """@see L{BrokerServer.send_message}"""

    @MethodCall.sender
    def is_message_pending(self, message_id):
        """@see L{BrokerServer.is_message_pending}"""

    @MethodCall.sender
    def stop_clients(self):
        """@see L{BrokerServer.stop_clients}"""

    @MethodCall.sender
    def reload_configuration(self):
        """@see L{BrokerServer.reload_configuration}"""

    @MethodCall.sender
    def register(self):
        """@see L{BrokerServer.register}"""

    @MethodCall.sender
    def get_accepted_message_types(self):
        """@see L{BrokerServer.get_accepted_message_types}"""

    @MethodCall.sender
    def get_server_uuid(self):
        """@see L{BrokerServer.get_server_uuid}"""

    @MethodCall.sender
    def register_client_accepted_message_type(self, type):
        """@see L{BrokerServer.register_client_accepted_message_type}"""

    @MethodCall.sender
    def exit(self):
        """@see L{BrokerServer.exit}"""

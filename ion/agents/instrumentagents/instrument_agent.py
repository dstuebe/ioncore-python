#!/usr/bin/env python

"""
@file ion/agents/instrumentagents/instrument_agent.py
@author Steve Foley
@author Edward Hunter
@brief Instrument Agent and client classes.
"""


import os
from uuid import uuid4

from twisted.internet import defer, reactor
try:
    import json
except:
    import simplejson as json

import ion.util.procutils as pu
import ion.util.ionlog
from ion.core.process.process import Process
from ion.core.process.process import ProcessClient
from ion.core.process.process import ProcessFactory
from ion.core.process.process import ProcessDesc
from ion.services.dm.distribution.events import InfoLoggingEventPublisher
from ion.services.dm.distribution.events \
    import BusinessStateModificationEventPublisher
from ion.services.dm.distribution.events import DataBlockEventPublisher
from ion.agents.instrumentagents.instrument_fsm import InstrumentFSM
from ion.agents.instrumentagents.instrument_constants import AgentParameter, \
    AgentConnectionState, AgentState, driver_client, \
    DriverAnnouncement, InstErrorCode, DriverParameter, DriverChannel, \
    ObservatoryState, DriverStatus, InstrumentCapability, DriverCapability, \
    MetadataParameter, AgentCommand, Datatype, TimeSource, ConnectionMethod, \
    AgentEvent, AgentStatus, ObservatoryCapability

log = ion.util.ionlog.getLogger(__name__)

DEBUG_PRINT = True if os.environ.get('DEBUG_PRINT',None) == 'True' else False

"""
Instrument agent observatory metadata.
"""
ci_param_metadata = {

    AgentParameter.EVENT_PUBLISHER_ORIGIN:
        {MetadataParameter.DATATYPE: Datatype.PUBSUB_ORIGIN,
         MetadataParameter.LAST_CHANGE_TIMESTAMP: (0, 0),
         MetadataParameter.FRIENDLY_NAME: 'Event Publisher Origin'},
    AgentParameter.TIME_SOURCE:
        {MetadataParameter.DATATYPE: Datatype.ENUM,
         MetadataParameter.LAST_CHANGE_TIMESTAMP: (0, 0),
         MetadataParameter.VALID_VALUES: TimeSource,
         MetadataParameter.FRIENDLY_NAME: 'Time Source'},
    AgentParameter.CONNECTION_METHOD:
        {MetadataParameter.DATATYPE: Datatype.ENUM,
         MetadataParameter.LAST_CHANGE_TIMESTAMP: (0, 0),
         MetadataParameter.VALID_VALUES: ConnectionMethod,
         MetadataParameter.FRIENDLY_NAME: 'Connection Method'},
    AgentParameter.DEFAULT_EXP_TIMEOUT:
        {MetadataParameter.DATATYPE: Datatype.INT,
         MetadataParameter.LAST_CHANGE_TIMESTAMP: (0, 0),
         MetadataParameter.MINIMUM_VALUE: 0,
         MetadataParameter.UNITS: 'Seconds',
         MetadataParameter.FRIENDLY_NAME: \
            'Default Transaction Expire Timeout'},
    AgentParameter.MAX_EXP_TIMEOUT:
        {MetadataParameter.DATATYPE: Datatype.INT,
         MetadataParameter.LAST_CHANGE_TIMESTAMP: (0, 0),
         MetadataParameter.MINIMUM_VALUE: 0,
         MetadataParameter.UNITS: 'Seconds',
         MetadataParameter.FRIENDLY_NAME: 'Max Transaction Expire Timeout'},
    AgentParameter.MAX_ACQ_TIMEOUT:
        {MetadataParameter.DATATYPE: Datatype.INT,
         MetadataParameter.LAST_CHANGE_TIMESTAMP: (0, 0),
         MetadataParameter.MINIMUM_VALUE: 0,
         MetadataParameter.UNITS: 'Seconds',
         MetadataParameter.FRIENDLY_NAME: 'Max Transaction Acquire Timeout'},
}


class InstrumentAgent(Process):
    """
    A generic ion representation of an instrument as an ION resource.
    """

    """
    The software version of the instrument agent.
    """
    version = '0.1.0'

    @classmethod
    def get_version(cls):
        """
        Return the software version of the instrument agent.
        """
        return cls.version

    @defer.inlineCallbacks
    def plc_init(self):
        # Initialize base class.
        Process.plc_init(self)

        # We need a yield in a inlineCallback.
        yield

        """
        The ID of the instrument this agent represents.
        """
        self.instrument_id = self.spawn_args.get('instrument-id', None)

        """
        Driver process and client descriptions. Parameter dictionaries
        used to launch driver processes, and dynamically construct driver
        client objects.
        """
        self._driver_desc = self.spawn_args.get('driver-desc', None)
        self._client_desc = self.spawn_args.get('client-desc', None)

        """
        The driver config dictionary. Default passed as a spawn arg.
        """
        self._driver_config = self.spawn_args.get('driver-config', None)

        """
        The driver process ID.
        """
        self._driver_pid = None

        """
        List of old driver processes to be cleaned up.
        """
        self._condemned_drivers = []

        """
        The driver client to communicate with the child driver.
        """
        self._driver_client = None

        """
        The PubSub origin for the event publisher that this instrument agent
        uses to distribute messages related to generic events that it handles.
        One queue sends all messages, each tagged with an event ID number and
        the "agent" keyword or channel name if applicable (delimited by ".").
        If "agent" keyword is used, the publication applies to the agent. If
        the channel is a "*", the event applies to the instrument as a whole or
        all channels on the instrument
        For example: 3003.chan1.machine_example_org_14491.357
        @see    ion/services/dm/distribution/events.py
        @see    https://confluence.oceanobservatories.org/display/
                syseng/CIAD+DM+SV+Notifications+and+Events
        """
        self.event_publisher_origin = str(self.id)

        """
        The PubSub publisher for informational/log events. These include
        agent op errors, transaction events, driver state changes, driver
        and agent config changes.
        """
        self._log_publisher = \
            InfoLoggingEventPublisher(process=self,
                                      origin=self.event_publisher_origin)

        """
        The PubSub publisher for data events
        """
        self._data_publisher = \
            DataBlockEventPublisher(process=self,
                                    origin=self.event_publisher_origin)

        """
        The PubSub publisher for agent state change events.
        """
        self._state_publisher = \
            BusinessStateModificationEventPublisher(process=self,
                                    origin=self.event_publisher_origin)

        """
        The transducer of the last data received event. Used to publish
        left over buffer contents on end of a streaming session.
        """
        self._prev_data_transducer = None

        """
        A UUID specifying the current transaction. None
        indicates no current transaction.
        """
        self.transaction_id = None

        """
        If a transaction expires during an op_ call, this flag is set so
        the transaction can be retired when finishing the call. It is handled
        there to keep the current operation protected until it completes.
        """
        self._transaction_timed_out = False

        """
        A twisted delayed function call that implements the transaction
        timeout. This object allows us to cancel the timeout when the
        transaction is ended before timeout.
        """
        self._transaction_timeout_call = None

        """
        A queue of pending transactions. Start the top one on the list when
        the current transaction ends.
        """
        self._pending_transactions = []

        """
        An integer in seconds for the maximum allowable timeout to wait for
        a new transaction.
        """
        self._max_acq_timeout = 60

        """
        An integer in seconds for the minimum time a transaction must be open.
        """
        self._min_exp_timeout = 1

        """
        An integer in seconds for the default time a transaction may be open.
        """
        self._default_exp_timeout = 300

        """
        An integer in seconds giving the maximum allowable time a transaction
        may be open.
        """
        self._max_exp_timeout = 1800

        """
        Upon transaction expire timeout, this flag indicates if the transaction
        can be immediately retired or should be flagged for retire upon
        completion of a protected operation.
        """
        self._in_protected_function = False

        """
        String indicating the source of time being used for the instrument.
        See time_sources list for available values.
        """
        self._time_source = TimeSource.NOT_SPECIFIED

        """
        String describing how the device is connected to the observatory.
        See connection_methods list for available values.
        """
        self._connection_method = ConnectionMethod.NOT_SPECIFIED

        """
        Buffer to hold instrument data for periodic transmission.
        """
        self._data_buffer = []

        """
        The number of samples to keep in the data buffer before publicaiton.
        """
        self._data_buffer_limit = 0

        """
        A dict of device capabilities that is read from the driver upon
        driver construction. The dict persists whether we are connected to
        the driver or not.
        """
        self._device_capabilities = {}

        """
        List of current alarm conditions. Tuple of (ID, description).
        """
        self._alarms = []

        """
        Dictionary of time status values.
        """
        self._time_status = {
            'Uncertainty': None,
            'Peers': None
        }

        """
        Agent state handlers
        """
        self._state_handlers = {
            AgentState.POWERED_DOWN: self.state_handler_powered_down,
            AgentState.UNINITIALIZED: self.state_handler_uninitialized,
            AgentState.INACTIVE: self.state_handler_inactive,
            AgentState.IDLE: self.state_handler_idle,
            AgentState.STOPPED: self.state_handler_stopped,
            AgentState.OBSERVATORY_MODE: self.state_handler_observatory_mode,
            AgentState.DIRECT_ACCESS_MODE: \
                self.state_handler_direct_access_mode
        }

        """
        A finite state machine to track and manage agent state according to
        the general instrument state model.
        """
        self._fsm = InstrumentFSM(AgentState, AgentEvent, self._state_handlers,
                                 AgentEvent.ENTER, AgentEvent.EXIT)

        # Set initial state.
        self._fsm.start(AgentState.UNINITIALIZED)

    ###########################################################################
    #   State handlers.
    ###########################################################################

    @defer.inlineCallbacks
    def state_handler_powered_down(self, event, params):
        """
        State handler for AgentState.POWERED_DOWN.
        This is a major state.
        TODO: Need to investigate use models of POWERED_DOWN.
        """

        yield
        success = InstErrorCode.OK
        next_state = None
        result = None
        self._debug_print(self._fsm.get_current_state(), event)

        if event == AgentEvent.ENTER:
            origin = 'agent.%s' % self.event_publisher_origin
            yield self._state_publisher.create_and_publish_event(origin=origin,
                                        description=AgentState.POWERED_DOWN)
            pass

        elif event == AgentEvent.EXIT:
            pass

        else:
            success = InstErrorCode.INCORRECT_STATE

        defer.returnValue((success, next_state, result))

    @defer.inlineCallbacks
    def state_handler_uninitialized(self, event, params):
        """
        State handler for AgentState.UNINITIALIZED.
        Substate of major state AgentState.POWERED_UP.
        """

        yield
        success = InstErrorCode.OK
        next_state = None
        result = None
        self._debug_print(self._fsm.get_current_state(), event)

        if event == AgentEvent.ENTER:
            # Low level agent initialization beyond construction and plc.
            origin = 'agent.%s' % self.event_publisher_origin
            yield self._state_publisher.create_and_publish_event(origin=origin,
                                        description=AgentState.UNINITIALIZED)
            pass

        elif event == AgentEvent.EXIT:
            pass

        elif event == AgentEvent.INITIALIZE:

            # Initialize: start driver and client and switch to INACTIVE
            # if successful.
            self._stop_condemned_drivers()
            yield self._start_driver()
            if self._driver_pid:
                next_state = AgentState.INACTIVE

            else:
                # Could not initialize error. Set error return value.
                success = InstErrorCode.AGENT_INIT_FAILED

            pass

        elif event == AgentEvent.RESET:
            next_state = AgentState.UNINITIALIZED

        else:
            success = InstErrorCode.INCORRECT_STATE

        defer.returnValue((success, next_state, result))

    @defer.inlineCallbacks
    def state_handler_inactive(self, event, params):
        """
        State handler for AgentState.INACTIVE.
        Substate of major state AgentState.POWERED_UP.
        """

        yield
        success = InstErrorCode.OK
        next_state = None
        result = None
        self._debug_print(self._fsm.get_current_state(), event)

        if event == AgentEvent.ENTER:
            # Agent initialization beyond driver spawn.
            origin = 'agent.%s' % self.event_publisher_origin
            yield self._state_publisher.create_and_publish_event(origin=origin,
                                        description=AgentState.INACTIVE)

            # Read the device capabilities.
            try:
                self._device_capabilities = {}
                reply = yield self._driver_client.get_capabilities(\
                                                [DriverCapability.DEVICE_ALL])
            except:
                pass

            else:
                result = reply['result']
                for (key, val) in result.iteritems():
                    success_val = val[0]
                    if InstErrorCode.is_ok(success_val):
                        self._device_capabilities[key] = val

        elif event == AgentEvent.EXIT:
            pass

        elif event == AgentEvent.RESET:
            self._stop_driver()
            if self._driver_pid == None:
                next_state = AgentState.UNINITIALIZED

            else:
                success = InstErrorCode.AGENT_DEINIT_FAILED

        elif event == AgentEvent.INITIALIZE:
            next_state = AgentState.INACTIVE

        elif event == AgentEvent.GO_ACTIVE:
            # Attempt to configure driver.
            reply = yield self._driver_client.configure(self._driver_config)
            success = reply['success']

            # If successful, attempt to connect.
            if InstErrorCode.is_ok(success):
                try:
                    success = None
                    reply = yield self._driver_client.connect()
                    success = reply['success']

                # Exception raised, reply error.
                except:
                    success = InstErrorCode.DRIVER_CONNECT_FAILED

                # Command returned, if successful switch state to IDLE.
                else:
                    if InstErrorCode.is_ok(success):
                        next_state = AgentState.IDLE

        else:
            success = InstErrorCode.INCORRECT_STATE

        defer.returnValue((success, next_state, result))

    @defer.inlineCallbacks
    def state_handler_stopped(self, event, params):
        """
        State handler for AgentState.STOPPED.
        Substate of major state AgentState.ACTIVE.
        """

        yield
        success = InstErrorCode.OK
        next_state = None
        result = None
        self._debug_print(self._fsm.get_current_state(), event)

        if event == AgentEvent.ENTER:
            # Save agent and driver running state.
            origin = 'agent.%s' % self.event_publisher_origin
            yield self._state_publisher.create_and_publish_event(origin=origin,
                                        description=AgentState.STOPPED)
            pass

        elif event == AgentEvent.EXIT:
            pass

        elif event == AgentEvent.CLEAR:
            next_state = AgentState.IDLE

        elif event == AgentEvent.RESUME:
            # Restore agent and driver running state.
            next_state = AgentState.OBSERVATORY_MODE

        elif event == AgentEvent.GO_INACTIVE:
            try:
                reply = yield self._driver_client.disconnect()
                success = reply['success']

            # Exception raised, reply error.
            except:
                success = InstErrorCode.DRIVER_CONNECT_FAILED

            # Command returned, if successful switch state to IDLE.
            else:
                if InstErrorCode.is_ok(success):
                    next_state = AgentState.INACTIVE

        elif event == AgentEvent.RESET:
            try:
                reply = yield self._driver_client.disconnect()
                success = reply['success']

            # Exception raised, reply error.
            except:
                success = InstErrorCode.DRIVER_CONNECT_FAILED

            # Command returned, shut down driver.
            else:
                if InstErrorCode.is_ok(success):
                    self._condemn_driver()

                    # If successful, switch to UNINITIALIZED.
                    if self._driver_pid == None:
                        next_state = AgentState.UNINITIALIZED

                    # If unsuccessful, switch to inactive.
                    else:
                        success = InstErrorCode.AGENT_DEINIT_FAILED
                        next_state = AgentState.INACTIVE

        else:
            success = InstErrorCode.INCORRECT_STATE

        defer.returnValue((success, next_state, result))

    @defer.inlineCallbacks
    def state_handler_idle(self, event, params):
        """
        State handler for AgentState.IDLE.
        Substate of major state AgentState.ACTIVE.
        """

        yield
        success = InstErrorCode.OK
        next_state = None
        result = None
        self._debug_print(self._fsm.get_current_state(), event)

        if event == AgentEvent.ENTER:
            # Clear agent and driver running state.
            origin = 'agent.%s' % self.event_publisher_origin
            yield self._state_publisher.create_and_publish_event(origin=origin,
                                        description=AgentState.IDLE)

        elif event == AgentEvent.EXIT:
            pass

        elif event == AgentEvent.GO_INACTIVE:
            try:
                reply = yield self._driver_client.disconnect()
                success = reply['success']

            # Exception raised, reply error.
            except:
                success = InstErrorCode.DRIVER_CONNECT_FAILED

            # Command returned, if successful switch state to IDLE.
            else:
                if InstErrorCode.is_ok(success):
                    next_state = AgentState.INACTIVE

        elif event == AgentEvent.RESET:
            try:
                reply = yield self._driver_client.disconnect()
                success = reply['success']

            # Exception raised, reply error.
            except:
                success = InstErrorCode.DRIVER_CONNECT_FAILED

            # Command returned, shut down driver.
            else:
                if InstErrorCode.is_ok(success):
                    self._condemn_driver()

                    # If successful, switch to UNINITIALIZED.
                    if self._driver_pid == None:
                        next_state = AgentState.UNINITIALIZED

                    # If unsuccessful, switch to inactive.
                    else:
                        success = InstErrorCode.AGENT_DEINIT_FAILED
                        next_state = AgentState.INACTIVE

        elif event == AgentEvent.RUN:
            next_state = AgentState.OBSERVATORY_MODE

        else:
            success = InstErrorCode.INCORRECT_STATE

        defer.returnValue((success, next_state, result))

    @defer.inlineCallbacks
    def state_handler_observatory_mode(self, event, params):
        """
        State handler for AgentState.OBSERVATORY_MODE.
        Substate of major state AgentState.ACTIVE.RUNNING.
        """

        yield
        success = InstErrorCode.OK
        next_state = None
        result = None
        self._debug_print(self._fsm.get_current_state(), event)

        if event == AgentEvent.ENTER:
            origin = 'agent.%s' % self.event_publisher_origin
            yield self._state_publisher.create_and_publish_event(origin=origin,
                    description=AgentState.OBSERVATORY_MODE)
            pass

        elif event == AgentEvent.EXIT:
            pass

        elif event == AgentEvent.CLEAR:
            next_state = AgentState.IDLE

        elif event == AgentEvent.PAUSE:
            next_state = AgentState.STOPPED

        elif event == AgentEvent.GO_DIRECT_ACCESS_MODE:
            next_state = AgentState.DIRECT_ACCESS_MODE

        elif event == AgentEvent.GO_INACTIVE:
            try:
                reply = yield self._driver_client.disconnect()
                success = reply['success']

            # Exception raised, reply error.
            except:
                success = InstErrorCode.DRIVER_CONNECT_FAILED

            # Command returned, if successful switch state to IDLE.
            else:
                if InstErrorCode.is_ok(success):
                    next_state = AgentState.INACTIVE

        elif event == AgentEvent.RESET:
            try:
                reply = yield self._driver_client.disconnect()
                success = reply['success']

            # Exception raised, reply error.
            except:
                success = InstErrorCode.DRIVER_DISCONNECT_FAILED

            # Command returned, shut down driver.
            else:
                if InstErrorCode.is_ok(success):
                    #yield pu.asleep(5)
                    self._condemn_driver()
                    # If successful, switch to UNINITIALIZED.
                    if self._driver_pid == None:
                        next_state = AgentState.UNINITIALIZED

                    # If unsuccessful, switch to inactive.
                    else:
                        success = InstErrorCode.AGENT_DEINIT_FAILED

                        next_state = AgentState.INACTIVE

        else:
            success = InstErrorCode.INCORRECT_STATE

        defer.returnValue((success, next_state, result))

    @defer.inlineCallbacks
    def state_handler_direct_access_mode(self, event, params):
        """
        State handler for AgentState.DIRECT_ACCESS_MODE.
        Substate of major state AgentState.ACTIVE.RUNNING.
        """

        yield
        success = InstErrorCode.OK
        next_state = None
        result = None
        self._debug_print(self._fsm.get_current_state(), event)

        if event == AgentEvent.ENTER:
            origin = 'agent.%s' % self.event_publisher_origin
            yield self._state_publisher.create_and_publish_event(origin=origin,
                            description=AgentState.DIRECT_ACCESS_MODE)

            # Launch the serial port emulator and return the connection info
            reply = yield self._LaunchSoCat()
            success = reply['success']
            result = reply['result']

        elif event == AgentEvent.EXIT:
            pass

        elif event == AgentEvent.GO_OBSERVATORY_MODE:
            next_state = AgentState.OBSERVATORY_MODE

        elif event == AgentEvent.GO_INACTIVE:
            try:
                reply = yield self._driver_client.disconnect()
                success = reply['success']

            # Exception raised, reply error.
            except:
                success = InstErrorCode.DRIVER_CONNECT_FAILED

            # Command returned, if successful switch state to IDLE.
            else:
                if InstErrorCode.is_ok(success):
                    next_state = AgentState.INACTIVE

        elif event == AgentEvent.RESET:
            try:
                reply = yield self._driver_client.disconnect()
                success = reply['success']

            # Exception raised, reply error.
            except:
                success = InstErrorCode.DRIVER_CONNECT_FAILED

            # Command returned, shut down driver.
            else:
                if InstErrorCode.is_ok(success):
                    self._condemn_driver()

                    # If successful, switch to UNINITIALIZED.
                    if self._driver_pid == None:
                        next_state = AgentState.UNINITIALIZED

                    # If unsuccessful, switch to inactive.
                    else:
                        success = InstErrorCode.AGENT_DEINIT_FAILED
                        next_state = AgentState.INACTIVE

        else:
            success = InstErrorCode.INCORRECT_STATE

        defer.returnValue((success, next_state, result))

    @defer.inlineCallbacks
    def _LaunchSoCat(self):
        """ """
        result = {'success': None, 'result': None}
        import os
        import socket
        import subprocess
        import tempfile
        import random

        # Open a null stream to pipe unwanted console messages to nowhere
        """
        tmpDir = tempfile.gettempdir()
        SERPORTMASTER = tmpDir +  '/serPortMaster'
        SERPORTSLAVE = tmpDir + '/serPortSlave'
        SOCATapp = 'socat'
        switch = '-d'
        SERPORTMODE = 'w+'
        NULLPORTMODE = 'w'
        port = random.randint (10000,60000)
        nullDesc = open (os.devnull, NULLPORTMODE)
        localName = socket.gethostname()
        agentIP = socket.gethostbyname (localName)
        master =  '-' #pty,link=' + SERPORTMASTER + ',raw,echo=0'
        slave = 'TCP-LISTEN:0' #pty,link=' + SERPORTSLAVE + ',raw,echo=0'
        print SOCATapp + '\n' + switch + '\n' + master + '\n' + slave
        vsp = subprocess.Popen("socat -d -d - TCP-LISTEN:0 &", shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            #vsp = subprocess.Popen([SOCATapp, switch, switch, master, slave, '&'], shell=False,stdout = subprocess.PIPE,stderr=subprocess.PIPE)
        return vsp.communicate(), vsp
        try:
            log.info('Creating virtual serial port. Running %s...' % SOCATapp)
            self._vsp = subprocess.Popen([SOCATapp, switch, switch, master, slave, '&'], stdout = subprocess.PIPE)
                                    #     stdout = nullDesc.fileno(), stderr = nullDesc.fileno(), shell =True)
        except OSError, e:
            log.error('Failure:  Could not create virtual serial port(s): %s' % e)
            return
        yield pu.asleep(1) # wait just a bit for connect
        if not os.path.exists(SERPORTMASTER) and os.path.exists(SERPORTSLAVE):
            log.error('Failure:  Unknown reason.')
            return
        log.debug('Successfully created virtual serial ports. socat PID: %d'
            % self._vsp.pid)
        self._serMaster = os.readlink(SERPORTMASTER)
        self._serSlave = os.readlink(SERPORTSLAVE)
        log.debug('Master port: %s   Slave port: %s' %(self._serMaster, self._serSlave))
        self._goodComms = True
        """
        result = {'success': InstErrorCode.NOT_IMPLEMENTED[0], 'result': InstErrorCode.NOT_IMPLEMENTED[1]}
        defer.returnValue (result)

    ###########################################################################
    #   Transaction Management
    ###########################################################################

    @defer.inlineCallbacks
    def op_start_transaction(self, content, headers, msg):
        """
        Begin an exclusive transaction with the agent.
        @param content A dict with None or nonnegative integer values
            'acq_timeout' and 'exp_timeout' for acquisition and expiration
            timeouts respectively.
        @retval A dict with 'success' success/fail string and
            'transaction_id' transaction ID UUID string.
        """

        assert(isinstance(content, dict)), 'Expected a dict content.'
        acq_timeout = content.get('acq_timeout', None)
        exp_timeout = content.get('exp_timeout', None)
        assert(acq_timeout == None or
               (isinstance(acq_timeout, int) and acq_timeout >= 0)), \
            'Expected None or nonnegative int acquisition timeout'
        assert(exp_timeout == None or
               (isinstance(exp_timeout, int)) and exp_timeout >= 0), \
            'Expected None or nonnegative int expiration timeout'

        result = {'success': None, 'transaction_id': None}

        (success, tid) = yield self._request_transaction(acq_timeout,
                                        exp_timeout, headers['sender'])
        result['success'] = success
        result['transaction_id'] = tid

        # Publish any errors.
        if InstErrorCode.is_error(success):
            desc_str = 'Error in op_start_transaction: ' + \
                       InstErrorCode.get_string(success)
            #origin = "agent.%s" % self.event_publisher_origin
            #yield self._log_publisher.create_and_publish_event(origin=origin,
            #    description=desc_str)

        else:
            desc_str = 'opened transaction %s' % tid

        origin = "agent.%s" % self.event_publisher_origin
        yield self._log_publisher.create_and_publish_event(origin=origin,
            description=desc_str)



        yield self.reply_ok(msg, result)

    def _start_transaction(self, exp_timeout):
        """
        Begin an exclusive transaction with the agent.
        @param exp_timeout An integer in seconds giving the allowable time
            the transaction may be open.
        @retval A tuple containing (success/fail, transaction ID UUID string).
        """

        assert(exp_timeout == None or
               (isinstance(exp_timeout, int)) and exp_timeout >= 0), \
            'Expected None or nonnegative int expiration timeout'

        # Ensure the expiration timeout is in the valid range.
        if exp_timeout == None:
            exp_timeout = self._default_exp_timeout
        elif exp_timeout > self._max_exp_timeout:
            exp_timeout = self._max_exp_timeout
        elif exp_timeout < self._min_exp_timeout:
            exp_timeout = self._min_exp_timeout

        # If the resource is free, issue a new transaction immediately.
        if self.transaction_id == None:
            self.transaction_id = str(uuid4())

            self._debug_print('started transaction', self.transaction_id)

            # Create and queue up a transaction expiration callback.
            def transaction_expired():
                """
                A callback to expire a transaction. Either retire
                the transaction directly (no protected call running), or set
                a flag for a protected call to do the cleanup when finishing.
                """

                self._debug_print('transaction expired', self.transaction_id)

                self._transaction_timeout_call = None
                if self._in_protected_function:
                    self._transaction_timed_out = True
                else:

                    self._end_transaction(self.transaction_id)

            self._transaction_timeout_call = reactor.callLater(exp_timeout,
                                                        transaction_expired)
            return (InstErrorCode.OK, self.transaction_id)

        # Otherwise return locked resource error.
        else:

            return (InstErrorCode.LOCKED_RESOURCE, None)

    def _request_transaction(self, acq_timeout, exp_timeout, requester):
        """
        @param acq_timeout An integer in seconds to wait to acquire a new
            transaction.
        @param exp_timeout An integer in seconds to allow the new transaction
            to remain open.
        @param requester A process ID for requester.
        @retval A deferred that will fire when the a new transaction has
            been constructed or timeout occurs. The deferred value is a
            tuple (success/fail, transaction_id).
        """

        assert(acq_timeout == None or
               (isinstance(acq_timeout, int) and acq_timeout >= 0)), \
            'Expected None or nonnegative int acquisition timeout'
        assert(exp_timeout == None or
               (isinstance(exp_timeout, int)) and exp_timeout >= 0), \
            'Expected None or nonnegative int expiration timeout'

        # Ensure the expiration timeout is in the valid range.
        if exp_timeout == None:
            exp_timeout = self._default_exp_timeout
        elif exp_timeout > self._max_exp_timeout:
            exp_timeout = self._max_exp_timeout
        elif exp_timeout < self._min_exp_timeout:
            exp_timeout = self._min_exp_timeout

        # Ensure the acquisition timeout is in the valid range.
        if acq_timeout == None:
            acq_timeout = 0
        elif acq_timeout > self._max_acq_timeout:
            acq_timeout = self._max_acq_timeout

        d = defer.Deferred()

        # If the resource is free, issue a new transaction immediately.
        if self.transaction_id == None:

            (success, tid) = self._start_transaction(exp_timeout)
            d.callback((success, tid))
            return d

        else:

            # If resourse not free and no acquisition timeout, return
            # locked error immediately.
            if acq_timeout == 0:
                d.callback((InstErrorCode.LOCKED_RESOURCE, None))
                return d

            # If resource not free and there is a valid acquisition timeout,
            # add the deferred return to the list of pending transactions and
            # start the acquisition timeout.

            self._debug_print('acquiring transaction')

            def acquisition_timeout():

                self._debug_print('acquire transaction timed out')

                for item in self._pending_transactions:
                    if item[0] == d:
                        self._pending_transactions.remove(item)
                        d.callback((InstErrorCode.TIMEOUT, None))

            acq_timeout_call = reactor.callLater(acq_timeout,
                                        acquisition_timeout)

            self._pending_transactions.append((d, acq_timeout_call,
                                        exp_timeout, requester))

            return d

    @defer.inlineCallbacks
    def op_end_transaction(self, content, headers, msg):
        """
        End the current transaction.
        @param content A uuid specifying the current transaction to end.
        @retval success/fail message.
        """

        tid = self.transaction_id

        result = self._end_transaction(content)

        # Publish an end transaction message...mainly as a test for now
        # yield self._log_publisher.create_and_publish_event(\
        #                                    name="Transaction ended!")

        # Publish any errors.
        success = result['success']
        if InstErrorCode.is_error(success):
            desc_str = 'Error in op_end_transaction: ' + \
                       InstErrorCode.get_string(success)
            #origin = "agent.%s" % self.event_publisher_origin
            #yield self._log_publisher.create_and_publish_event(origin=origin,
            #    description=desc_str)

        else:
            desc_str = 'closed transaction %s' % tid

        origin = "agent.%s" % self.event_publisher_origin
        yield self._log_publisher.create_and_publish_event(origin=origin,
            description=desc_str)

        yield self.reply_ok(msg, result)

    def _end_transaction(self, tid):
        """
        End the current transaction and start the next pending transaction
            if one is waiting.
        @param tid A uuid specifying the current transaction to end.
        @retval success/fail message.
        """

        assert(isinstance(tid, str)), 'Expected a str transaction ID.'

        result = {'success': None}

        if tid == self.transaction_id:

            self._debug_print('ending transaction', self.transaction_id)

            # Remove the current transaction.
            self.transaction_id = None

            # Reset expiration flag and cancel expiration timeout.
            self._transaction_timed_out = False
            if self._transaction_timeout_call != None:
                self._transaction_timeout_call.cancel()
                self._transaction_timeout_call = None

            # If there is a pending transaction, issue a new transaction
            # and cancel the acquisition timeout.
            if len(self._pending_transactions) > 0:
                (d, call, exp_timeout, requester) = \
                    self._pending_transactions.pop(0)
                call.cancel()
                (success, tid) = self._start_transaction(exp_timeout)
                d.callback((success, tid))

            # Return success.
            result['success'] = InstErrorCode.OK

        # If there is no transaction to end, return not locked error.
        elif self.transaction_id == None:
            result['success'] = InstErrorCode.RESOURCE_NOT_LOCKED

        # If the tid does not match the current trasaction, return
        # locked error.
        else:
            result['success'] = InstErrorCode.LOCKED_RESOURCE

        return result

    @defer.inlineCallbacks
    def _verify_transaction(self, tid, optype):
        """
        Verify the passed transaction ID is currently open, or open an
        implicit transaction.
        @param tid 'create' to create an implicit transaction, 'none' to
            perform the operation without a transaction, or a UUID to test
            against the current transaction ID.
        @param optype 'get' 'set' or 'execute'
        @retval True if the transaction is valid or if one was successfully
            created, False otherwise.
        """

        assert(isinstance(tid, str)), 'Expected transaction ID str.'
        assert(isinstance(optype, str)), 'Expected str optype.'

        success = None
        if tid != 'create' and tid != 'none' and len(tid) != 36:
            success = InstErrorCode.INVALID_TRANSACTION_ID

        # Try to start an implicit transaction if tid is 'create'
        elif tid == 'create':
            (success, tid) = self._start_transaction(self._default_exp_timeout)

            if InstErrorCode.is_ok(success):
                success = InstErrorCode.OK

            else:
                success = InstErrorCode.LOCKED_RESOURCE

        # Allow only gets without a current or created transaction.
        elif tid == 'none' and self.transaction_id == None and optype == 'get':
            success = InstErrorCode.OK

        # Otherwise, the given ID must match the outstanding one
        elif (tid == self.transaction_id):
            success = InstErrorCode.OK

        else:
            success = InstErrorCode.LOCKED_RESOURCE

        # Publish any errors.
        if InstErrorCode.is_error(success):
            desc_str = 'Error in verify_transaction: ' + \
                       InstErrorCode.get_string(success)
            origin = "agent.%s" % self.event_publisher_origin
            yield self._log_publisher.create_and_publish_event(origin=origin,
                description=desc_str)

        defer.returnValue(success)

    ###########################################################################
    #   Observatory Facing Interface
    ###########################################################################

    @defer.inlineCallbacks
    def op_hello(self, content, headers, msg):

        # The following line shows how to reply to a message
        yield self.reply_ok(msg, {'value': 'Hello there, ' + str(content)}, {})

    @defer.inlineCallbacks
    def op_execute_observatory(self, content, headers, msg):
        """
        Execute infrastructure commands related to the Instrument Agent
        instance. This includes commands for messaging, resource management
        processes, etc.
        @param content A dict {'command': [command, arg, ,arg],
            'transaction_id': transaction_id)}
        @retval ACK message containing a dict
            {'success': success, 'result': command-specific,
            'transaction_id': transaction_id}.
        """

        self._in_protected_function = True

        assert(isinstance(content, dict)), 'Expected a dict content.'
        assert('command' in content), 'Expected a command.'
        assert('transaction_id' in content),'Expected a transaction_id.'

        cmd = content['command']
        tid = content['transaction_id']

        assert(isinstance(cmd, (tuple, list))), 'Expected a command \
            list or tuple.'
        assert(isinstance(tid, str)), 'Expected a transaction_id str.'

        reply = {'success': None, 'result': None, 'transaction_id': None}

        # Set up the transaction.
        success = yield self._verify_transaction(tid, 'execute')
        if InstErrorCode.is_error(success):
            reply['success'] = success
            yield self.reply_ok(msg, reply)
            return

        reply['transaction_id'] = self.transaction_id

        success = None
        result = None

        try:
            # TRANSITION command.
            if  cmd[0] == AgentCommand.TRANSITION:

                # Verify required parameter present.
                if len(cmd) < 2:
                    success = InstErrorCode.REQUIRED_PARAMETER

                # Verify required parameter valid.
                elif not AgentEvent.has(cmd[1]):
                    success = InstErrorCode.INVALID_PARAM_VALUE

                else:
                    (success, result) = yield self._fsm.on_event_async(cmd[1])

            # TRANSMIT DATA command.
            elif cmd[0] == AgentCommand.TRANSMIT_DATA:
                success = InstErrorCode.NOT_IMPLEMENTED

            # SLEEP command.
            elif cmd[0] == AgentCommand.SLEEP:
                if len(cmd) < 2:
                    success = InstErrorCode.REQUIRED_PARAMETER

                else:
                    Ltime = cmd[1]
                    if not isinstance(Ltime, int):
                        success = InstErrorCode.INVALID_PARAM_VALUE

                    elif Ltime <= 0:
                        success = InstErrorCode.INVALID_PARAM_VALUE
                    else:
                        yield pu.asleep(Ltime)
                        success = InstErrorCode.OK

            else:
                success = InstErrorCode.UNKNOWN_COMMAND

        # Unknown error.
        except:
            success = InstErrorCode.UNKNOWN_ERROR
            raise

        # Set reply values.
        else:
            reply['success'] = success

        # Publish errors, clean up transaction.
        finally:

            if success == None:
                desc_str = 'Error in op_execute_observatory: ' + \
                    InstErrorCode.get_string(InstErrorCode.UNKNOWN_ERROR)
                origin = "agent.%s" % self.event_publisher_origin
                yield self._log_publisher.create_and_publish_event(origin=\
                                            origin, description=desc_str)

            elif InstErrorCode.is_error(success):
                desc_str = 'Error in op_execute_observatory: ' + \
                    InstErrorCode.get_string(success)
                origin = "agent.%s" % self.event_publisher_origin
                yield self._log_publisher.create_and_publish_event(origin=\
                                            origin, description=desc_str)

            if (tid == 'create') or (self._transaction_timed_out == True):
                self._end_transaction(self.transaction_id)
            self._in_protected_function = False
        yield self.reply_ok(msg, reply)

    @defer.inlineCallbacks
    def op_get_observatory(self, content, headers, msg):
        """
        Get data from the cyberinfrastructure side of the agent (registry info,
        topic locations, messaging parameters, process parameters, etc.)
        @param content A dict {'params': [param_arg, ,param_arg],
            'transaction_id': transaction_id}.
        @retval A reply message containing a dict
            {'success': success, 'result': {param_arg: (success, val), ...,
            param_arg: (success, val)}, 'transaction_id': transaction_id)
        """
        self._in_protected_function = True
        assert(isinstance(content, dict)), 'Expected a dict content.'
        assert('params' in content), 'Expected params.'
        assert('transaction_id' in content), 'Expected a transaction_id.'
        params = content['params']
        tid = content['transaction_id']
        assert(isinstance(params, (tuple, list))), 'Expected a parameter \
            list or tuple.'
        assert(isinstance(tid, str)), 'Expected a transaction_id str.'
        reply = {'success': None, 'result': None, 'transaction_id': None}
        # Set up the transaction
        success = yield self._verify_transaction(tid, 'get')
        if InstErrorCode.is_error(success):
            reply['success'] = success
            yield self.reply_ok(msg, reply)
            return

        reply['transaction_id'] = self.transaction_id
        result = {}
        get_errors = False

        try:
            # Add each observatory parameter given in params list.
            for arg in params:
                if not AgentParameter.has(arg):
                    result[arg] = (InstErrorCode.INVALID_PARAMETER, None)
                    get_errors = True
                    continue

                if arg == AgentParameter.EVENT_PUBLISHER_ORIGIN or \
                    arg == AgentParameter.ALL:
                    if self.event_publisher_origin == None:
                        result[AgentParameter.EVENT_PUBLISHER_ORIGIN] = \
                            (InstErrorCode.OK, None)
                    else:
                        result[AgentParameter.EVENT_PUBLISHER_ORIGIN] = \
                            (InstErrorCode.OK, self.event_publisher_origin)

                if arg == AgentParameter.DRIVER_DESC or \
                    arg == AgentParameter.ALL:
                    result[AgentParameter.DRIVER_DESC] = \
                        (InstErrorCode.OK, self._driver_desc)

                if arg == AgentParameter.DRIVER_CLIENT_DESC or \
                    arg == AgentParameter.ALL:
                    result[AgentParameter.DRIVER_CLIENT_DESC] = \
                        (InstErrorCode.OK, self._client_desc)

                if arg == AgentParameter.DRIVER_CONFIG or \
                    arg == AgentParameter.ALL:
                    result[AgentParameter.DRIVER_CONFIG] = \
                        (InstErrorCode.OK, self._driver_config)

                if arg == AgentParameter.TIME_SOURCE or \
                    arg == AgentParameter.ALL:
                    result[AgentParameter.TIME_SOURCE] = \
                        (InstErrorCode.OK, self._time_source)

                if arg == AgentParameter.CONNECTION_METHOD or \
                    arg == AgentParameter.ALL:
                    result[AgentParameter.CONNECTION_METHOD] = \
                        (InstErrorCode.OK, self._connection_method)

                if arg == AgentParameter.MAX_ACQ_TIMEOUT or \
                    arg == AgentParameter.ALL:
                    result[AgentParameter.MAX_ACQ_TIMEOUT] = \
                        (InstErrorCode.OK, self._max_acq_timeout)

                if arg == AgentParameter.DEFAULT_EXP_TIMEOUT or \
                    arg == AgentParameter.ALL:
                    result[AgentParameter.DEFAULT_EXP_TIMEOUT] = \
                        (InstErrorCode.OK, self._default_exp_timeout)

                if arg == AgentParameter.MAX_EXP_TIMEOUT or \
                    arg == AgentParameter.ALL:
                    result[AgentParameter.MAX_EXP_TIMEOUT] = \
                        (InstErrorCode.OK, self._max_exp_timeout)

                if arg == AgentParameter.BUFFER_SIZE or \
                    arg == AgentParameter.ALL:
                    result[AgentParameter.BUFFER_SIZE] = \
                        (InstErrorCode.OK, self._data_buffer_limit)

        # Unknown error.
        except:
            success = InstErrorCode.UNKNOWN_ERROR
            raise

        # Set reply values.
        else:
            if get_errors:
                success = InstErrorCode.GET_OBSERVATORY_ERR

            else:
                success = InstErrorCode.OK

            reply['success'] = success
            reply['result'] = result

        # Publish errors, clean up transaction.
        finally:

            # Publish any errors.
            if InstErrorCode.is_error(success):
                desc_str = 'Error in op_get_observatory: ' + \
                           InstErrorCode.get_string(success)
                origin = "agent.%s" % self.event_publisher_origin
                yield self._log_publisher.create_and_publish_event(origin=\
                                            origin, description=desc_str)

            # Transaction clean up. End implicit or expired transactions.
            if (tid == 'create') or (self._transaction_timed_out == True):
                self._end_transaction(self.transaction_id)
            self._in_protected_function = False

        yield self.reply_ok(msg, reply)

    @defer.inlineCallbacks
    def op_set_observatory(self, content, headers, msg):
        """
        Set parameters related to the infrastructure side of the agent
        (registration information, location, network addresses, etc.)
        @param content A dict {'params': {param_arg: val, ..., param_arg: val},
            'transaction_id': transaction_id}.
        @retval Reply message with dict
            {'success': success, 'result': {param_arg: success, ...,
                param_arg: success}, 'transaction_id': transaction_id}.
        """

        self._in_protected_function = True

        assert(isinstance(content, dict)), 'Expected a dict content.'
        assert('params' in content), 'Expected params.'
        assert('transaction_id' in content), 'Expected a transaction_id.'

        params = content['params']
        tid = content['transaction_id']

        assert(isinstance(params, dict)), 'Expected a parameter dict.'
        assert(isinstance(tid, str)), 'Expected a transaction_id str.'

        reply = {'success': None, 'result': None, 'transaction_id': None}

        # Set up the transaction
        success = yield self._verify_transaction(tid, 'set')
        if InstErrorCode.is_error(success):
            reply['success'] = success
            yield self.reply_ok(msg, reply)
            return

        reply['transaction_id'] = self.transaction_id
        result = {}
        set_errors = False
        set_successes = False

        try:

            # Add each observatory parameter given in params list.
            # Note: it seems like all the current params should be read only by
            # general agent users.
            for arg in params.keys():
                if not AgentParameter.has(arg):
                    result[arg] = InstErrorCode.INVALID_PARAMETER
                    set_errors = True
                    continue

                val = params[arg]

                if arg == AgentParameter.DRIVER_DESC:
                    if not isinstance(val, dict):
                        # Better checking here.
                        result[arg] = InstErrorCode.INVALID_PARAM_VALUE
                        set_errors = True

                    else:
                        self._driver_desc = val
                        result[arg] = InstErrorCode.OK
                        set_successes = True

                elif arg == AgentParameter.DRIVER_CLIENT_DESC:
                    if not isinstance(val, dict):
                        # Better checking here.
                        result[arg] = InstErrorCode.INVALID_PARAM_VALUE
                        set_errors = True

                    else:
                        self._client_desc = val
                        result[arg] = InstErrorCode.OK
                        set_successes = True

                elif arg == AgentParameter.DRIVER_CONFIG:
                    if not isinstance(val, dict):
                        # Better checking here.
                        result[arg] = InstErrorCode.INVALID_PARAM_VALUE
                        set_errors = True

                    else:
                        self._driver_config = val
                        result[arg] = InstErrorCode.OK
                        set_successes = True

                elif arg == AgentParameter.TIME_SOURCE:
                    if TimeSource.has(val):
                        if val != self._time_source:
                            self._time_source = val
                            # Logic here when new time source set.
                            # And test for successful switch.
                            result[arg] = InstErrorCode.OK
                            set_successes = True

                        else:
                            result[arg] = InstErrorCode.OK

                    else:
                        set_errors = True
                        result[arg] = InstErrorCode.INVALID_PARAM_VALUE

                elif arg == AgentParameter.CONNECTION_METHOD:
                    if ConnectionMethod.has(val):
                        if val != self._connection_method:
                            self._connection_method = val
                            # Logic here when new connection method set.
                            # And test for successful switch.
                            result[arg] = InstErrorCode.OK
                            set_successes = True

                        else:
                            result[arg] = InstErrorCode.OK

                    else:
                        result[arg] = InstErrorCode.INVALID_PARAM_VALUE

                elif arg == AgentParameter.MAX_ACQ_TIMEOUT:
                    if isinstance(val, int) and val >= 0:
                        self._max_acq_timeout = val
                        result[arg] = InstErrorCode.OK
                        set_successes = True

                    else:
                        set_errors = True
                        result[arg] = InstErrorCode.INVALID_PARAM_VALUE

                elif arg == AgentParameter.DEFAULT_EXP_TIMEOUT:
                    if isinstance(val, int) and val >= self._min_exp_timeout \
                        and val <= self._max_exp_timeout:
                        self._default_exp_timeout = val
                        result[arg] = InstErrorCode.OK
                        set_successes = True

                    else:
                        set_errors = True
                        result[arg] = InstErrorCode.INVALID_PARAM_VALUE

                elif arg == AgentParameter.MAX_EXP_TIMEOUT:
                    if isinstance(val, int) and val > self._min_exp_timeout:
                        self._max_exp_timeout = val
                        result[arg] = InstErrorCode.OK
                        set_successes = True

                    else:
                        set_errors = True
                        result[arg] = InstErrorCode.INVALID_PARAM_VALUE

                elif arg == AgentParameter.BUFFER_SIZE:
                    if isinstance(val, int) and val >= 0:
                        self._data_buffer_limit = val
                        result[arg] = InstErrorCode.OK
                        set_successes = True

                    else:
                        set_errors = True
                        result[arg] = InstErrorCode.INVALID_PARAM_VALUE

        # Unknown error.
        except:
            success = InstErrorCode.UNKNOWN_ERROR
            raise

        # Set reply values.
        else:
            if set_errors:
                success = InstErrorCode.GET_OBSERVATORY_ERR

            else:
                success = InstErrorCode.OK

            reply['success'] = success
            reply['result'] = result

        # Publish errors, clean up transaction.
        finally:

            # Publish any errors.
            if InstErrorCode.is_error(success):
                desc_str = 'Error in op_set_observatory: ' + \
                           InstErrorCode.get_string(success)
                origin = "agent.%s" % self.event_publisher_origin
                yield self._log_publisher.create_and_publish_event(origin=\
                                            origin, description=desc_str)

            # Publish the new agent configuration.
            if set_successes:
                origin = "agent.%s" % self.event_publisher_origin
                config = self._get_parameters()
                # strval = self._get_data_string(config)
                json_val = json.dumps(config)
                yield self._log_publisher.create_and_publish_event(origin=\
                                            origin, description=json_val)

            if (tid == 'create') or (self._transaction_timed_out == True):
                self._end_transaction(self.transaction_id)
            self._in_protected_function = False

        yield self.reply_ok(msg, reply)

    @defer.inlineCallbacks
    def op_get_observatory_metadata(self, content, headers, msg):
        """
        Retrieve metadata about the observatory configuration parameters.
        @param content A dict
            {'params': [(param_arg, meta_arg), ..., (param_arg, meta_arg)],
            'transaction_id': transaction_id}
        @retval A reply message with a dict {'success': success,
            'result': {(param_arg, meta_arg): (success, val), ...,
                (param_arg,meta_arg): (success, val)}
                'transaction_id': transaction_id}.
        """

        self._in_protected_function = True

        assert(isinstance(content, dict)), 'Expected a dict content.'
        assert('params' in content), 'Expected params.'
        assert('transaction_id' in content), 'Expected a transaction_id.'

        params = content['params']
        tid = content['transaction_id']

        assert(isinstance(params, (tuple, list))), 'Expected a parameter \
            list or tuple.'
        assert(isinstance(tid, str)), 'Expected a transaction_id str.'

        reply = {'success': None, 'result': None, 'transaction_id': None}

        # Set up the transaction
        success = yield self._verify_transaction(tid, 'get')
        if InstErrorCode.is_error(success):
            reply['success'] = success
            yield self.reply_ok(msg, reply)
            return

        reply['transaction_id'] = self.transaction_id

        try:
            pass

        # Unknown error.
        except:
            success = InstErrorCode.UNKNOWN_ERROR
            raise

        # Set reply values.
        else:
            success = InstErrorCode.NOT_IMPLEMENTED
            reply['success'] = success

        # Publish errors, clean up transaction.
        finally:

            # Publish any errors.
            if InstErrorCode.is_error(success):
                desc_str = 'Error in op_get_observatory_metadata: ' + \
                           InstErrorCode.get_string(success)
                origin = "agent.%s" % self.event_publisher_origin
                yield self._log_publisher.create_and_publish_event(origin=\
                                            origin, description=desc_str)

            if (tid == 'create') or (self._transaction_timed_out == True):
                self._end_transaction(self.transaction_id)
            self._in_protected_function = False

        yield self.reply_ok(msg, reply)

    @defer.inlineCallbacks
    def op_get_observatory_status(self, content, headers, msg):
        """
        Retrieve the observatory status values, including lifecycle state and
        other dynamic observatory status values indexed by status keys.
        @param content A dict {'params': [status_arg, ..., status_arg],
            'transaction_id': transaction_id}.
        @retval Reply message with a dict
            {'success': success, 'result': {status_arg: (success, val), ...,
            status_arg: (success, val)}, 'transaction_id': transaction_id}
        """

        self._in_protected_function = True
        assert(isinstance(content, dict)), 'Expected a dict content.'
        assert('params' in content), 'Expected params.'
        assert('transaction_id' in content), 'Expected a transaction_id.'

        params = content['params']
        tid = content['transaction_id']

        assert(isinstance(params, (tuple, list))), 'Expected a parameter \
            list or tuple.'
        assert(isinstance(tid, str)), 'Expected a transaction_id str.'

        reply = {'success': None, 'result': None, 'transaction_id': None}

        # Set up the transaction
        success = yield self._verify_transaction(tid, 'get')
        if InstErrorCode.is_error(success):
            reply['success'] = success
            yield self.reply_ok(msg, reply)
            return

        reply['transaction_id'] = self.transaction_id
        get_errors = False
        result = {}

        try:

            # Set up the result message.
            for arg in params:

                # If status key not recognized, report error.
                if not AgentStatus.has(arg):
                    result[arg] = (InstErrorCode.INVALID_STATUS, None)
                    get_errors = True
                    continue

                # Agent state.
                if arg == AgentStatus.AGENT_STATE or arg == AgentStatus.ALL:
                    result[AgentStatus.AGENT_STATE] = \
                        (InstErrorCode.OK, self._fsm.get_current_state())

                # Connection state.
                if arg == AgentStatus.CONNECTION_STATE or arg == \
                    AgentStatus.ALL:
                    result[AgentStatus.CONNECTION_STATE] = \
                        (InstErrorCode.OK, self._get_connection_state())

                # Alarm conditions.
                if arg == AgentStatus.ALARMS or arg == AgentStatus.ALL:
                    result[AgentStatus.ALARMS] = \
                        (InstErrorCode.OK, self._alarms)

                # Time status.
                if arg == AgentStatus.TIME_STATUS or arg == AgentStatus.ALL:
                    result[AgentStatus.TIME_STATUS] = \
                        (InstErrorCode.OK, self._time_status)

                # Data buffer size.
                if arg == AgentStatus.BUFFER_SIZE or arg == AgentStatus.ALL:
                    result[AgentStatus.BUFFER_SIZE] = \
                        (InstErrorCode.OK, self._get_buffer_size())

                # Agent software version.
                if arg == AgentStatus.AGENT_VERSION or arg == AgentStatus.ALL:
                    result[AgentStatus.AGENT_VERSION] = \
                        (InstErrorCode.OK, self.get_version())

                # Pending transactions.
                if arg == AgentStatus.PENDING_TRANSACTIONS or arg == \
                    AgentStatus.ALL:
                    pending_transaction_pids = \
                        [item[3] for item in self._pending_transactions]
                    result[AgentStatus.PENDING_TRANSACTIONS] = \
                        (InstErrorCode.OK, pending_transaction_pids)

        # Unknown error.
        except:
            success = InstErrorCode.UNKNOWN_ERROR
            raise

        # Set reply values.
        else:
            if get_errors:
                success = InstErrorCode.GET_OBSERVATORY_ERR
            else:
                success = InstErrorCode.OK

            reply['success'] = success
            reply['result'] = result

        # Publish errors, clean up transaction.
        finally:

            # Publish any errors.
            if InstErrorCode.is_error(success):
                desc_str = 'Error in op_get_observatory_status: ' + \
                           InstErrorCode.get_string(success)
                origin = "agent.%s" % self.event_publisher_origin
                yield self._log_publisher.create_and_publish_event(origin=\
                                            origin, description=desc_str)

            if (tid == 'create') or (self._transaction_timed_out == True):
                self._end_transaction(self.transaction_id)
            self._in_protected_function = False

        yield self.reply_ok(msg, reply)

    @defer.inlineCallbacks
    def op_get_capabilities(self, content, headers, msg):
        """
        Retrieve the agent capabilities, including observatory and
        device values, both common and specific to the agent / device.
        @param content A dict {'params': [cap_arg, ..., cap_arg],
            'transaction_id': transaction_id}.
        @retval Reply message with a dict {'success': success,
            'result': {cap_arg: (success, [cap_val, ..., cap_val]), ...,
            cap_arg: (success, [cap_val, ..., cap_val])},
            'transaction_id': transaction_id}
        """

        self._in_protected_function = True

        assert(isinstance(content, dict)), 'Expected a dict content.'
        assert('params' in content), 'Expected params.'
        assert('transaction_id' in content), 'Expected a transaction_id.'

        params = content['params']
        tid = content['transaction_id']

        assert(isinstance(params, (tuple, list))), 'Expected a parameter list \
            or tuple.'
        assert(isinstance(tid, str)), 'Expected a transaction_id str.'

        reply = {'success': None, 'result': None, 'transaction_id': None}

        # Set up the transaction
        success = yield self._verify_transaction(tid, 'get')
        if InstErrorCode.is_error(success):
            reply['success'] = success
            yield self.reply_ok(msg, reply)
            return

        reply['transaction_id'] = self.transaction_id
        get_errors = False
        result = {}

        try:

            # Do the work here.
            # Set up the result message.
            for arg in params:
                if not InstrumentCapability.has(arg):
                    result[arg] = (InstErrorCode.INVALID_CAPABILITY, None)
                    get_errors = True
                    continue

                if ObservatoryCapability.has(arg) or arg == \
                    InstrumentCapability.ALL:

                    if arg == InstrumentCapability.OBSERVATORY_COMMANDS or \
                            arg == InstrumentCapability.OBSERVATORY_ALL or \
                            arg == InstrumentCapability.ALL:
                        result[InstrumentCapability.OBSERVATORY_COMMANDS] = \
                            (InstErrorCode.OK, AgentCommand.list())

                    if arg == InstrumentCapability.OBSERVATORY_PARAMS or \
                            arg == InstrumentCapability.OBSERVATORY_ALL or \
                            arg == InstrumentCapability.ALL:
                        result[InstrumentCapability.OBSERVATORY_PARAMS] = \
                            (InstErrorCode.OK, AgentParameter.list())

                    if arg == InstrumentCapability.OBSERVATORY_STATUSES or \
                            arg == InstrumentCapability.OBSERVATORY_ALL or \
                            arg == InstrumentCapability.ALL:
                        result[InstrumentCapability.OBSERVATORY_STATUSES] = \
                            (InstErrorCode.OK, AgentStatus.list())

                    if arg == InstrumentCapability.OBSERVATORY_METADATA or \
                            arg == InstrumentCapability.OBSERVATORY_ALL or \
                            arg == InstrumentCapability.ALL:
                        result[InstrumentCapability.OBSERVATORY_METADATA] = \
                            (InstErrorCode.OK, MetadataParameter.list())

                if DriverCapability.has(arg) or arg == \
                    InstrumentCapability.ALL:

                    if arg == InstrumentCapability.DEVICE_CHANNELS or \
                           arg == InstrumentCapability.DEVICE_ALL or \
                           arg == InstrumentCapability.ALL:
                        val = self._device_capabilities.\
                              get(InstrumentCapability.DEVICE_CHANNELS, None)
                        if val != None:
                            result[InstrumentCapability.DEVICE_CHANNELS] = \
                                (InstErrorCode.OK, val)
                        else:
                            result[InstrumentCapability.DEVICE_CHANNELS] = \
                                (InstErrorCode.INVALID_CAPABILITY, None)
                            get_errors = True

                    if arg == InstrumentCapability.DEVICE_COMMANDS or \
                           arg == InstrumentCapability.DEVICE_ALL or \
                           arg == InstrumentCapability.ALL:
                        val = self._device_capabilities.\
                              get(InstrumentCapability.DEVICE_COMMANDS, None)
                        if val != None:
                            result[InstrumentCapability.DEVICE_COMMANDS] = \
                                (InstErrorCode.OK, val)
                        else:
                            result[InstrumentCapability.DEVICE_COMMANDS] = \
                                (InstErrorCode.INVALID_CAPABILITY, None)
                            get_errors = True

                    if arg == InstrumentCapability.DEVICE_METADATA or \
                           arg == InstrumentCapability.DEVICE_ALL or \
                           arg == InstrumentCapability.ALL:
                        val = self._device_capabilities.\
                              get(InstrumentCapability.DEVICE_METADATA, None)
                        if val != None:
                            result[InstrumentCapability.DEVICE_METADATA] = \
                                (InstErrorCode.OK, val)
                        else:
                            result[InstrumentCapability.DEVICE_METADATA] = \
                                (InstErrorCode.INVALID_CAPABILITY, None)
                            get_errors = True

                    if arg == InstrumentCapability.DEVICE_PARAMS or \
                           arg == InstrumentCapability.DEVICE_ALL or \
                           arg == InstrumentCapability.ALL:
                        val = self._device_capabilities.\
                              get(InstrumentCapability.DEVICE_PARAMS, None)
                        if val != None:
                            result[InstrumentCapability.DEVICE_PARAMS] = \
                                (InstErrorCode.OK, val)
                        else:
                            result[InstrumentCapability.DEVICE_PARAMS] = \
                                (InstErrorCode.INVALID_CAPABILITY, None)
                            get_errors = True

                    if arg == InstrumentCapability.DEVICE_STATUSES or \
                           arg == InstrumentCapability.DEVICE_ALL or \
                           arg == InstrumentCapability.ALL:
                        val = self._device_capabilities.\
                              get(InstrumentCapability.DEVICE_STATUSES, None)
                        if val != None:
                            result[InstrumentCapability.DEVICE_STATUSES] = \
                                (InstErrorCode.OK, val)
                        else:
                            result[InstrumentCapability.DEVICE_STATUSES] = \
                                (InstErrorCode.INVALID_CAPABILITY, None)
                            get_errors = True

        # Unkonwn error.
        except:
            success = InstErrorCode.UNKNOWN_ERROR
            raise

        # Set reply values.
        else:
            if get_errors:
                success = InstErrorCode.GET_OBSERVATORY_ERR
            else:
                success = InstErrorCode.OK

            reply['success'] = success
            reply['result'] = result

        # Publish errors, clean up transaction.
        finally:

            # Publish any errors.
            if InstErrorCode.is_error(success):
                desc_str = 'Error in op_get_observatory_capabilities: ' + \
                           InstErrorCode.get_string(success)
                origin = "agent.%s" % self.event_publisher_origin
                yield self._log_publisher.create_and_publish_event(origin=\
                                            origin, description=desc_str)

            if (tid == 'create') or (self._transaction_timed_out == True):
                self._end_transaction(self.transaction_id)
            self._in_protected_function = False

        yield self.reply_ok(msg, reply)

    ###########################################################################
    #   Instrument Facing Interface
    ###########################################################################

    @defer.inlineCallbacks
    def op_execute_device(self, content, headers, msg):
        """
        Execute a command on the device fronted by the agent. Commands may be
        common or specific to the device, with specific commands known through
        knowledge of the device or a previous get_capabilities query.
        @param content A dict
            {'channels': [chan_arg, ..., chan_arg],
            'command': [command, arg, ..., argN],
            'transaction_id': transaction_id}
        @retval A reply message with a dict
            {'success': success,
            'result': {chan_arg: (success, command_specific),
            ..., chan_arg: (success, command_specific_values)},
            'transaction_id': transaction_id}.
        """

        self._in_protected_function = True

        assert(isinstance(content, dict)), 'Expected a dict content.'
        assert('channels' in content), 'Expected channels.'
        assert('command' in content), 'Expected command.'
        assert('transaction_id' in content), 'Expected a transaction_id.'

        channels = content['channels']
        command = content['command']
        tid = content['transaction_id']

        assert(isinstance(channels, (tuple, list))), 'Expected a channels \
            list or tuple.'
        assert(isinstance(command, (tuple, list))), 'Expected a command list \
            or tuple.'
        assert(isinstance(tid, str)), 'Expected a transaction_id str.'

        reply = {'success': None, 'result': None, 'transaction_id': None}

        # Set up the transaction
        success = yield self._verify_transaction(tid, 'execute')
        if InstErrorCode.is_error(success):
            reply['success'] = success
            yield self.reply_ok(msg, reply)
            return

        reply['transaction_id'] = self.transaction_id

        agent_state = self._fsm.get_current_state()
        if agent_state != AgentState.OBSERVATORY_MODE:
            reply['success'] = InstErrorCode.INCORRECT_STATE
            yield self.reply_ok(msg, reply)
            return

        timeout = 60
        success = None
        result = None

        try:

            dvr_result = yield self._driver_client.execute(channels, command,
                                                          timeout)
            success = dvr_result.get('success', None)
            result = dvr_result.get('result', None)

        # Unknown error.
        except:
            success = InstErrorCode.UNKNOWN_ERROR
            raise

        # Set reply values.
        else:
            reply['success'] = success
            reply['result'] = result

        # Publish errors, clean up transaction.
        finally:

            # Publish any errors.
            if InstErrorCode.is_error(success):
                desc_str = 'Error in op_execute_device: ' + \
                           InstErrorCode.get_string(success)
                origin = "agent.%s" % self.event_publisher_origin
                yield self._log_publisher.create_and_publish_event(origin\
                                            =origin, description=desc_str)

            if (tid == 'create') or (self._transaction_timed_out == True):
                self._end_transaction(self.transaction_id)
            self._in_protected_function = False

        yield self.reply_ok(msg, reply)

    @defer.inlineCallbacks
    def op_get_device(self, content, headers, msg):
        """
        Get configuration parameters from the instrument.
        @param content A dict {'params': [(chan_arg, param_arg), ...,
            (chan_arg, param_arg)], 'transaction_id': transaction_id}
        @retval A reply message with a dict
            {'success': success,
            'result': {(chan_arg, param_arg): (success, val),
            ..., (chan_arg, param_arg): (success, val)},
            'transaction_id': transaction_id}
        """

        self._in_protected_function = True

        assert(isinstance(content, dict)), 'Expected a dict content.'
        assert('params' in content), 'Expected params.'
        assert('transaction_id' in content), 'Expected a transaction_id.'

        params = content['params']
        tid = content['transaction_id']

        assert(isinstance(params, (tuple, list))), 'Expected a parameter \
            list or tuple.'
        assert(isinstance(tid, str)), 'Expected a transaction_id str.'

        reply = {'success': None, 'result': None, 'transaction_id': None}

        # Set up the transaction
        success = yield self._verify_transaction(tid, 'get')
        if InstErrorCode.is_error(success):
            reply['success'] = success
            yield self.reply_ok(msg, reply)
            return

        reply['transaction_id'] = self.transaction_id

        agent_state = self._fsm.get_current_state()
        if agent_state != AgentState.OBSERVATORY_MODE and \
                          agent_state != AgentState.IDLE and \
                          agent_state != AgentState.STOPPED:
            reply['success'] = InstErrorCode.INCORRECT_STATE
            yield self.reply_ok(msg, reply)
            return

        timeout = 60
        success = None
        result = None

        try:

            dvr_result = yield self._driver_client.get(params,timeout)
            success = dvr_result.get('success', None)
            result = dvr_result.get('result', None)
            #pass

        # Unkonwn error.
        except:
            success = InstErrorCode.UNKNOWN_ERROR
            raise

        # Set reply values.
        else:
            reply['success'] = success
            reply['result'] = result
            #reply['success'] = InstErrorCode.OK
            #reply['result'] = {'parameter': (InstErrorCode.OK, 'value')}

        # Publish errors, clean up transaction.
        finally:

            if InstErrorCode.is_error(success):
                desc_str = 'Error in op_get_device: ' + \
                           InstErrorCode.get_string(success)
                origin = "agent.%s" % self.event_publisher_origin
                yield self._log_publisher.create_and_publish_event(origin=\
                                            origin, description=desc_str)

            if (tid == 'create') or (self._transaction_timed_out == True):
                self._end_transaction(self.transaction_id)
            self._in_protected_function = False

        yield self.reply_ok(msg, reply)

    @defer.inlineCallbacks
    def op_set_device(self, content, headers, msg):
        """
        Set parameters to the instrument side of of the agent.
        @param content A dict {'params': {(chan_arg, param_arg): val, ...,
            (chan_arg, param_arg): val}, 'transaction_id': transaction_id}.
        @retval Reply message with a dict
            {'success': success, 'result': {(chan_arg, param_arg): success,
            ..., chan_arg, param_arg): success},
            'transaction_id': transaction_id}.
        """

        self._in_protected_function = True

        assert(isinstance(content, dict)), 'Expected a dict content.'
        assert('params' in content), 'Expected params.'
        assert('transaction_id' in content), 'Expected a transaction_id.'

        params = content['params']
        tid = content['transaction_id']

        assert(isinstance(params, dict)), 'Expected a parameter dict.'
        assert(isinstance(tid, str)), 'Expected a transaction_id str.'

        reply = {'success': None, 'result': None, 'transaction_id': None}

        # Set up the transaction
        success = yield self._verify_transaction(tid, 'set')
        if InstErrorCode.is_error(success):
            reply['success'] = success
            yield self.reply_ok(msg, reply)
            return

        reply['transaction_id'] = self.transaction_id

        agent_state = self._fsm.get_current_state()
        if agent_state != AgentState.OBSERVATORY_MODE:
            reply['success'] = InstErrorCode.INCORRECT_STATE
            yield self.reply_ok(msg, reply)
            return

        timeout = 60
        success = None
        result = None

        try:

            dvr_result = yield self._driver_client.set(params,timeout)
            success = dvr_result.get('success', None)
            result = dvr_result.get('result', None)

        # Unknown error.
        except:
            success = InstErrorCode.UNKNOWN_ERROR
            raise

        # Set reply values.
        else:
            reply['success'] = success
            reply['result'] = result

        # Publish errors, clean up transaction.
        finally:

            if InstErrorCode.is_error(success):
                desc_str = 'Error in op_execute_device: ' + \
                           InstErrorCode.get_string(success)
                origin = "agent.%s" % self.event_publisher_origin
                yield self._log_publisher.create_and_publish_event(origin=\
                                            origin, description=desc_str)

            if (tid == 'create') or (self._transaction_timed_out == True):
                self._end_transaction(self.transaction_id)
            self._in_protected_function = False

        yield self.reply_ok(msg, reply)

    @defer.inlineCallbacks
    def op_execute_device_direct(self, content, headers, msg):
        """
        Execute untranslated byte data commands on the device.
        Must be in direct access mode.
        @param content A dict {'bytes': bytes}
        @retval A dict {'success': success, 'result': result}.
        """

        self._in_protected_function = True

        assert(isinstance(content, dict)), 'Expected a dict content.'
        assert('bytes' in content), 'Expected bytes.'
        assert('transaction_id' in content), 'Expected a transaction_id.'

        bytes = content['bytes']
        tid = content['transaction_id']

        assert(isinstance(bytes, str)), 'Expected a bytes string.'
        assert(isinstance(tid, str)), 'Expected a transaction_id str.'

        reply = {'success': None, 'result': None, 'transaction_id': None}

        # Set up the transaction
        success = yield self._verify_transaction(tid, 'execute')
        if InstErrorCode.is_error(success):
            reply['success'] = success
            yield self.reply_ok(msg, reply)
            return

        reply['transaction_id'] = self.transaction_id

        agent_state = self._fsm.get_current_state()
        if agent_state != AgentState.DIRECT_ACCESS_MODE:
            reply['success'] = InstErrorCode.INCORRECT_STATE
            yield self.reply_ok(msg, reply)
            return

        timeout = 60
        success = None
        result = None

        try:

            dvr_result = yield self._driver_client.execute_direct(bytes,
                                                                  timeout)
            success = dvr_result.get('success', None)
            result = dvr_result.get('result', None)

        # Unknown error.
        except:
            success = InstErrorCode.UNKOWN_ERROR
            raise

        # Set reply values.
        else:
            reply['success'] = success
            reply['result'] = result

        # Publish errors, clean up transaction.
        finally:

            # Publish any errors.
            if InstErrorCode.is_error(success):
                desc_str = 'Error in op_execute_device_direct: ' + \
                           InstErrorCode.get_string(success)
                origin = "agent.%s" % self.event_publisher_origin
                yield self._log_publisher.create_and_publish_event(origin=\
                                            origin, description=desc_str)

            if (tid == 'create') or (self._transaction_timed_out == True):
                self._end_transaction(self.transaction_id)
            self._in_protected_function = False

        yield self.reply_ok(msg, reply)

    @defer.inlineCallbacks
    def op_get_device_metadata(self, content, headers, msg):
        """
        Retrieve metadata for the device, its transducers and parameters.
        @param content A dict {'params': [(chan_arg, param_arg, meta_arg), ...,
            (chan_arg, param_arg, meta_arg)], 'transaction_id': transaction_id}
        @retval Reply message with a dict
            {'success': success,
            'result': {(chan_arg, param_arg, meta_arg): (success, val),
            ..., chan_arg, param_arg, meta_arg): (success, val)},
            'transaction_id': transaction_id}.
        """

        self._in_protected_function = True

        assert(isinstance(content, dict)), 'Expected a dict content.'
        assert('params' in content), 'Expected params.'
        assert('transaction_id' in content), 'Expected a transaction_id.'

        params = content['params']
        tid = content['transaction_id']

        assert(isinstance(params, (tuple, list))), 'Expected a parameter \
            list or tuple.'
        assert(isinstance(tid, str)), 'Expected a transaction_id str.'

        reply = {'success': None, 'result': None, 'transaction_id': None}

        # Set up the transaction
        success = yield self._verify_transaction(tid, 'get')
        if InstErrorCode.is_error(success):
            reply['success'] = success
            yield self.reply_ok(msg, reply)
            return

        reply['transaction_id'] = self.transaction_id

        agent_state = self._fsm.get_current_state()
        if agent_state != AgentState.OBSERVATORY_MODE and \
                          agent_state != AgentState.IDLE and \
                          agent_state != AgentState.STOPPED:
            reply['success'] = InstErrorCode.INCORRECT_STATE
            yield self.reply_ok(msg, reply)
            return

        timeout = 60
        success = None
        result = None

        try:

            dvr_content = {'params': params}
            dvr_result = yield self._driver_client.get_metadata(dvr_content,
                                                                timeout)
            success = dvr_result.get('success', None)
            result = dvr_result.get('result', None)

        # Unkown error.
        except:
            success = InstErrorCode.UNKNOWN_ERROR

        # Set reply values.
        else:
            reply['success'] = success
            reply['result'] = result

        # Publish errors, clean up transaction.
        finally:

            # Publish any errors.
            if InstErrorCode.is_error(success):
                desc_str = 'Error in op_get_device_metadata: ' + \
                           InstErrorCode.get_string(success)
                origin = "agent.%s" % self.event_publisher_origin
                yield self._log_publisher.create_and_publish_event(origin=\
                                            origin, description=desc_str)

            if (tid == 'create') or (self._transaction_timed_out == True):
                self._end_transaction(self.transaction_id)
            self._in_protected_function = False

        yield self.reply_ok(msg, reply)

    @defer.inlineCallbacks
    def op_get_device_status(self, content, headers, msg):
        """
        Obtain the status of an instrument. This includes non-parameter
        and non-lifecycle state of the instrument.
        @param content A dict {'params': [(chan_arg, status_arg), ...,
            chan_arg, status_arg)], 'transaction_id': transaction_id}.
        @retval A reply message with a dict
            {'success': success,
            'result': {(chan_arg, status_arg): (success, val), ...,
            (chan_arg, status_arg): (success, val)},
            'transaction_id': transaction_id}.
        """

        self._in_protected_function = True

        assert(isinstance(content, dict)), 'Expected a dict content.'
        assert('params' in content), 'Expected params.'
        assert('transaction_id' in content), 'Expected a transaction_id.'

        params = content['params']
        tid = content['transaction_id']

        assert(isinstance(params, (tuple, list))), 'Expected a parameter \
            list or tuple.'
        assert(isinstance(tid, str)), 'Expected a transaction_id str.'

        reply = {'success': None, 'result': None, 'transaction_id': None}

        # Set up the transaction
        success = yield self._verify_transaction(tid, 'get')
        if InstErrorCode.is_error(success):
            reply['success'] = success
            yield self.reply_ok(msg, reply)
            return

        reply['transaction_id'] = self.transaction_id

        agent_state = self._fsm.get_current_state()
        if agent_state != AgentState.OBSERVATORY_MODE and \
                          agent_state != AgentState.IDLE and \
                          agent_state != AgentState.STOPPED:
            reply['success'] = InstErrorCode.INCORRECT_STATE
            yield self.reply_ok(msg, reply)
            return

        timeout = 60
        success = None
        result = None

        try:

            dvr_content = {'params': params}
            dvr_result = yield self._driver_client.get_status(dvr_content,
                                                              timeout)
            success = dvr_result.get('success', None)
            result = dvr_result.get('result', None)

        # Unknown error.
        except:
            success = InstErrorCode.UNKNOWN_ERROR

        # Set reply values.
        else:
            reply['success'] = success
            reply['result'] = result

        # Publish errors, clean up transaction.
        finally:

            # Publish any errors.
            if InstErrorCode.is_error(success):
                desc_str = 'Error in op_get_device_metadata: ' + \
                           InstErrorCode.get_string(success)
                origin = "agent.%s" % self.event_publisher_origin
                yield self._log_publisher.create_and_publish_event(origin=\
                                            origin, description=desc_str)

            if (tid == 'create') or (self._transaction_timed_out == True):
                self._end_transaction(self.transaction_id)
            self._in_protected_function = False

        yield self.reply_ok(msg, reply)

    ###########################################################################
    #   Publishing Methods
    ###########################################################################

    @defer.inlineCallbacks
    def op_driver_event_occurred(self, content, headers, msg):
        """
        Called by the driver to announce the occurance of an event. The agent
        take appropriate action including state transitions, data formatting
        and publication. This method must be called by a child process of the
        agent.
        @param content a dict with 'type' and 'transducer' strings and 'value'
            object.
        """
        log.debug("op_driver_event_occurred begins")
        assert isinstance(content, dict), 'Expected a content dict.'

        type = content.get('type', None)
        transducer = content.get('transducer', None)
        value = content.get('value', None)

        assert isinstance(type, str), 'Expected a type string.'
        assert isinstance(transducer, str), 'Expected a transducer string.'
        assert value != None, 'Expected a value.'

        if not (self._is_child_process(headers['sender-name'])):
            yield self.reply_err(msg,
                        'driver event occured evoked from a non-child process')
            return

        # If data received, coordinate buffering and publishing.
        if type == DriverAnnouncement.DATA_RECEIVED:
            # Remember the transducer in case we need to transmit at a time
            # other than these events.
            self._prev_data_transducer = transducer

            # Get the driver observatory state.
            key = (DriverChannel.INSTRUMENT, DriverStatus.OBSERVATORY_STATE)
            reply = yield self._driver_client.get_status([key])
            success = reply['success']
            result = reply['result']
            obs_status = result.get(key, None)
            json_val = None

            # If in streaming mode, buffer data and publish at intervals.
            if InstErrorCode.is_ok(success) and obs_status != None:
                if obs_status[1] == ObservatoryState.STREAMING:
                    self._data_buffer.append(value)
                    if len(self._data_buffer) > self._data_buffer_limit:
                        # strval = self._get_data_string(self._data_buffer)
                        json_val = json.dumps(self._data_buffer)
                        self._data_buffer = []

                # If not in streaming mode, always publish data upon receipt.
                else:
                    #strval = self._get_data_string(value)
                    json_val = json.dumps([value])

            #if len(strval) > 0:
            if json_val != None:
                origin = "%s.%s" % (transducer, self.event_publisher_origin)
                log.debug("Instrument Agent publishing data: %s on origin: %s", json_val, origin)
                yield self._data_publisher.create_and_publish_event(\
                    origin=origin, data_block=json_val)

        # Driver configuration changed, publish config.
        elif type == DriverAnnouncement.CONFIG_CHANGE:

            reply = yield self._driver_client.get([(DriverChannel.ALL,
                                                    DriverParameter.ALL)])
            success = reply['success']
            result = reply['result']
            if InstErrorCode.is_ok(success) and len(result) > 0:
                str_key_dict = {}
                for (key, val) in result.iteritems():
                    str_key = '%s__%s' % (key[0], key[1])
                    str_key_dict[str_key] = val
                #strval = self._get_data_string(result)
                json_val = json.dumps(str_key_dict)
                origin = "%s.%s" % (transducer, self.event_publisher_origin)
                yield self._log_publisher.create_and_publish_event(origin=\
                                            origin, description=json_val)

        elif type == DriverAnnouncement.ERROR:
            pass

        # If the driver state changed, publish any buffered data remaining.
        elif type == DriverAnnouncement.STATE_CHANGE:
            json_val = None
            if len(self._data_buffer) > 0:
                #strval = self._get_data_string(self._data_buffer)
                json_val = json.dumps(self._data_buffer)
                #if len(strval) > 0:
                if json_val != None:
                    origin = "%s.%s" % (self._prev_data_transducer,
                                        self.event_publisher_origin)
                    yield self._log_publisher.create_and_publish_event(origin=\
                                                origin, description=json_val)

        elif type == DriverAnnouncement.EVENT_OCCURRED:
            pass

        else:
            pass

        self._debug_print_driver_event(type, transducer, value)


    ###########################################################################
    #   Driver lifecycle.
    ###########################################################################

    @defer.inlineCallbacks
    def _start_driver(self):
        """
        Spawn the driver and dynamically construct the client from the
        current description dictionaries.
        @retval True if both the client and driver were successfully
            created, False otherwise.
        """

        if self._client_desc and \
            'module' in self._client_desc  and \
            'class' in self._client_desc and \
            self._driver_desc and \
            'module' in self._driver_desc and \
            'class' in self._driver_desc and \
            'name' in self._driver_desc:
            import_str = 'from ' + self._client_desc['module'] + \
            ' import ' + self._client_desc['class']

            # Spawn the driver process.
            try:
                proc_desc = ProcessDesc(**(self._driver_desc))
                self.temp_proc_desc = proc_desc
                self._driver_pid = yield self.spawn_child(proc_desc)

            # If the process desc is bad, trap the error and proceed.
            # Do not construct client or set member objects.
            except ImportError:
                pass

            # Process spawn successful, start client, set member objects.
            else:
                self._debug_print('started driver', str(self._driver_pid))

                # Dynamically construct the client object
                ctor_str = 'driver_client = ' + self._client_desc['class'] + \
                    '(proc=self, target=self._driver_pid)'

                try:
                    exec import_str
                    exec ctor_str

                # Client import is bad, shutdown driver and exit.
                except ImportError, NameError:
                    log.info('Client import was bad, shutting down driver: %s' \
                        % NameError)
                    self._stop_driver()

                # Other error, shutdown driver and raise.
                except:
                    self._stop_driver()
                    raise

                # Driver and client constructed. Set client object.
                else:
                    self._driver_client = driver_client
                    self._debug_print('constructed driver client',
                                      str(self._driver_client))

    def _condemn_driver(self):
        """
        Add current driver to a list to be shutdown at a convenient time.
        Destroy the client object.
        """

        if self._driver_pid != None:
            self._condemned_drivers.append(self._driver_pid)
            self._driver_pid = None
            self._driver_client = None

    def _stop_condemned_drivers(self):
        """
        Shutdown any old driver processes.
        """

        new_children = []
        for item in self.child_procs:
            if item.proc_id in self._condemned_drivers:
                self._debug_print('shutting down driver', str(item.proc_id))
                self.shutdown_child(item)
            else:
                new_children.append(item)
        self.child_procs = new_children

    def _stop_driver(self):
        """
        Shutdown the driver and driver client processes.
        """

        # Shutdown the driver process and remove its reference.
        if self._driver_pid != None:

            # Add code to correctly shut down the child proc.
            for item in self.child_procs:
                if item.proc_id == self._driver_pid:
                    self._debug_print('shutting down driver',
                                      str(self._driver_pid))
                    self.shutdown_child(item)
                    self.child_procs.remove(item)

            self._driver_pid = None
            self._driver_client = None

    ###########################################################################
    #   Other.
    ###########################################################################

    def _get_connection_state(self):
        """
        @retval The current connection state of the agent, including
            connection to a remote-side agent, existence of a driver,
            and connection to instrument hardware. Should be extended to
            handle cases where there is a persistent shoreside and
            intermittant wetside agent component.
        """

        curstate = self._fsm.get_current_state()

        if curstate == AgentState.POWERED_DOWN:
            return AgentConnectionState.POWERED_DOWN
        elif curstate == AgentState.UNINITIALIZED:
            return AgentConnectionState.NO_DRIVER
        elif curstate == AgentState.INACTIVE:
            return AgentConnectionState.DISCONNECTED
        elif curstate == AgentState.IDLE:
            return AgentConnectionState.CONNECTED
        elif curstate == AgentState.STOPPED:
            return AgentConnectionState.CONNECTED
        elif curstate == AgentState.OBSERVATORY_MODE:
            return AgentConnectionState.CONNECTED
        elif curstate == AgentState.DIRECT_ACCESS_MODE:
            return AgentConnectionState.CONNECTED
        elif curstate == AgentState.UNKNOWN:
            return AgentConnectionState.UNKOWN
        else:
            return AgentConnectionState.UNKOWN

    def _is_child_process(self, name):
        """
        Determine if a process with the given name is a child process
        @param name The name to test for subprocess-ness
        @retval True if the name matches a child process name, False otherwise
        """
        log.debug("__is_child_process looking for process '%s' in %s",
                  name, self.child_procs)
        found = False
        for proc in self.child_procs:
            if proc.proc_name == name:
                found = True
                break
        return found

    def _get_buffer_size(self):
        """
        Return the total size in characters of the data buffer.
        Assumes the buffer is a list of string data lines.
        """
        return sum(map(lambda x: len(x), self._data_buffer))

    def _get_data_string(self, data):
        """
        Convert a sample dictionary or list of sample dictionaries into a
        publishable string value.
        @param data A dictionary containing an instrument data sample in
            key-value pairs or a list of such dictionaries representing a
            buffered set of samples.
        @retval A string representation of the data to be published.
        """

        assert(isinstance(data, (list, tuple, dict))), 'Expected a data dict, \
            or a list or tuple of data dicts'

        if isinstance(data, dict):
            return str(data)
        else:
            strval = ''
            for item in data:
                strval += str(item) + ','

            strval = strval[:-1]
            return strval

    def _get_parameters(self):
        """
        Get a dictionary of agent parameter values.
        @retval A dict containing the agent parameters as key-value pairs.
        """

        params = {}
        params[AgentParameter.EVENT_PUBLISHER_ORIGIN] = \
            self.event_publisher_origin
        params[AgentParameter.TIME_SOURCE] = self._time_source
        params[AgentParameter.CONNECTION_METHOD] = self._connection_method
        params[AgentParameter.MAX_ACQ_TIMEOUT] = self._max_acq_timeout
        params[AgentParameter.DEFAULT_EXP_TIMEOUT] = self._default_exp_timeout
        params[AgentParameter.MAX_EXP_TIMEOUT] = self._max_exp_timeout
        params[AgentParameter.DRIVER_DESC] = self._driver_desc
        params[AgentParameter.DRIVER_CLIENT_DESC] = self._client_desc
        params[AgentParameter.DRIVER_CONFIG] = self._driver_config
        params[AgentParameter.BUFFER_SIZE] = self._data_buffer_limit
        return params

    def _debug_print_driver_event(self, type, transducer, value):
        """
        Print debug driver events to stdio.
        @param type String event type.
        @param transducer String transducer producing the event.
        @param value Value of the event.
        """
        log.debug('Driver event type: %s, transducer: %s, value: %s',
                  type, transducer, value)


    def _debug_print(self, event=None, value=None):
        """
        Print debug agent events to stdio.
        @param event String event type.
        @param value String event value.
        """
        log.debug("Event: %s, value: %s", event, value)


class InstrumentAgentClient(ProcessClient):
    """
    Agent client class provides RPC messaging to the agent service.
    """

    # Increased rpc timeout for agent operations.
    default_rpc_timeout = 180

    ###########################################################################
    #   Transaction Management.
    ###########################################################################

    @defer.inlineCallbacks
    def start_transaction(self, acq_timeout=None, exp_timeout=None):
        """
        Begin an exclusive transaction with the agent.
        @param acq_timeout An integer in seconds to wait for the transaction.
        @param exp_timeout An integer in seconds to expire the transaction.
        @retval Transaction ID UUID string.
        """

        assert(acq_timeout is None or isinstance(acq_timeout, int)), \
            'Expected int or None acquisition timeout.'
        assert(exp_timeout is None or isinstance(exp_timeout, int)), \
            'Expected int or None expire timeout.'

        params = {
            'acq_timeout': acq_timeout,
            'exp_timeout': exp_timeout
        }

        if acq_timeout is not None and acq_timeout > 0:
            rpc_timeout = acq_timeout + 10
            (content, headers, message) = \
                yield self.rpc_send('start_transaction', params,
                                    timeout=rpc_timeout)

        else:
            (content, headers, message) = \
                yield self.rpc_send('start_transaction', params,
                                    timeout=self.default_rpc_timeout)

        assert(isinstance(content, dict)), 'Expected dict result'

        defer.returnValue(content)

    @defer.inlineCallbacks
    def end_transaction(self, tid):
        """
        End the current transaction.
        @param tid A uuid string specifying the current transaction to end.
        """
        assert(isinstance(tid, str)), 'Expected a transaction_id str.'
        (content, headers, message) = yield self.rpc_send('end_transaction',
                                                          tid)
        #yield pu.asleep(1)
        assert(isinstance(content, dict)), 'Expected dict result'
        defer.returnValue(content)

    ###########################################################################
    #   Observatory Facing Interface.
    ###########################################################################

    @defer.inlineCallbacks
    def hello(self, text='Hi there'):
        yield self._check_init()
        (content, headers, msg) = yield self.rpc_send('hello', text)
        defer.returnValue(str(content))

    @defer.inlineCallbacks
    def execute_observatory(self, command, transaction_id):
        """
        Execute infrastructure commands related to the Instrument Agent
        instance. This includes commands for messaging, resource management
        processes, etc.
        @param command A command list [command, arg, ,arg].
        @param transaction_id A transaction_id uuid4 or string 'create,' or
            'none.'
        @retval Reply dict {'success': success, 'result': command-specific,
            'transaction_id': transaction_id}.
        """

        assert(isinstance(command, list)), 'Expected a command list.'
        assert(isinstance(transaction_id, str)), \
            'Expected a transaction_id str.'

        yield self._check_init()
        content = {'command': command, 'transaction_id': transaction_id}
        (content, headers, messaage) = yield \
            self.rpc_send('execute_observatory', content,
                          timeout=self.default_rpc_timeout)

        assert(isinstance(content, dict))
        defer.returnValue(content)

    @defer.inlineCallbacks
    def get_observatory(self, params, transaction_id='none'):
        """
        Get data from the cyberinfrastructure side of the agent (registry info,
        topic locations, messaging parameters, process parameters, etc.)
        @param params A paramter list [param_arg, ,param_arg].
        @param transaction_id A transaction ID uuid4 or string 'create,'
            or 'none.'
        @retval A reply dict {'success': success,
            'result': {param_arg: (success, val), ...,
                param_arg: (success, val)},'transaction_id': transaction_id}.
        """

        assert(isinstance(params, list)), 'Expected a parameter list.'
        assert(isinstance(transaction_id, str)), \
            'Expected a transaction_id str.'

        yield self._check_init()
        content = {'params': params, 'transaction_id': transaction_id}
        (content, headers, messaage) = yield \
            self.rpc_send('get_observatory', content,
                          timeout=self.default_rpc_timeout)

        assert(isinstance(content, dict))
        defer.returnValue(content)

    @defer.inlineCallbacks
    def set_observatory(self, params, transaction_id='none'):
        """
        Set parameters related to the infrastructure side of the agent
        (registration information, location, network addresses, etc.)
        @param params A parameter-value dict {'params': {param_arg: val,
            ..., param_arg: val}.
        @param transaction_id A transaction ID uuid4 or string 'create,' or
            'none.'
        @retval Reply dict
            {'success': success,
            'result': {param_arg: success, ..., param_arg: success},
            'transaction_id': transaction_id}.
        """
        assert(isinstance(params, dict)), 'Expected a parameter-value dict.'
        assert(isinstance(transaction_id, str)), \
            'Expected a transaction_id str.'

        yield self._check_init()
        content = {'params': params, 'transaction_id': transaction_id}
        (content, headers, messaage) = yield \
            self.rpc_send('set_observatory', content,
                          timeout=self.default_rpc_timeout)

        assert(isinstance(content, dict))
        defer.returnValue(content)

    @defer.inlineCallbacks
    def get_observatory_metadata(self, params, transaction_id='none'):
        """
        Retrieve metadata about the observatory configuration parameters.
        @param params A metadata parameter list [(param_arg, meta_arg), ...,
            (param_arg, meta_arg)].
        @param transaction_id A transaction ID uuid4 or string 'create,'
            or 'none.'
        @retval A reply dict {'success': success,
            'result': {(param_arg, meta_arg): (success, val), ...,
                (param_arg, meta_arg): (success, val)},
            'transaction_id': transaction_id}.
        """
        assert(isinstance(params, list)), 'Expected a parameter list.'
        assert(isinstance(transaction_id, str)), \
            'Expected a transaction_id str.'

        yield self._check_init()
        content = {'params': params, 'transaction_id': transaction_id}
        (content, headers, messaage) = yield \
            self.rpc_send('get_observatory_metadata', content,
                          timeout=self.default_rpc_timeout)

        assert(isinstance(content, dict))
        defer.returnValue(content)

    @defer.inlineCallbacks
    def get_observatory_status(self, params, transaction_id='none'):
        """
        Retrieve the observatory status values, including lifecycle state
        and other dynamic observatory status values indexed by status keys.
        @param params A parameter list [status_arg, ..., status_arg].
        @param transaction_id A transaction ID uuid4 or string 'create,'
            or 'none.'
        @retval Reply dict
            {'success': success, 'result': {status_arg: (success, val), ...,
                status_arg: (success, val)}, 'transaction_id': transaction_id}
        """
        assert(isinstance(params, list)), 'Expected a parameter list.'
        assert(isinstance(transaction_id, str)), \
            'Expected a transaction_id str.'

        yield self._check_init()
        content = {'params': params, 'transaction_id': transaction_id}
        (content, headers, messaage) = yield \
            self.rpc_send('get_observatory_status', content,
                          timeout=self.default_rpc_timeout)

        assert(isinstance(content, dict))
        defer.returnValue(content)

    @defer.inlineCallbacks
    def get_capabilities(self, params, transaction_id='none'):
        """
        Retrieve the agent capabilities, including observatory and
        device values, both common and specific to the agent / device.
        @param params A parameter list [cap_arg, ..., cap_arg].
        @param transaction_id A transaction ID uuid4 or string 'create,'
            or 'none.'
        @retval Reply dict {'success': success,
            'result': {cap_arg: (success, val), ...,cap_arg: (success, val)},
            'transaction_id': transaction_id}
        """
        assert(isinstance(params, list)), 'Expected a parameter list.'
        assert(isinstance(transaction_id, str)), \
            'Expected a transaction_id str.'

        yield self._check_init()
        content = {'params': params, 'transaction_id': transaction_id}
        (content, headers, messaage) = yield \
            self.rpc_send('get_capabilities', content,
                          timeout=self.default_rpc_timeout)

        assert(isinstance(content, dict))
        defer.returnValue(content)

    ###########################################################################
    #   Instrument Facing Interface.
    ###########################################################################

    @defer.inlineCallbacks
    def execute_device(self, channels, command, transaction_id='none'):
        """
        Execute a command on the device fronted by the agent. Commands may be
        common or specific to the device, with specific commands known through
        knowledge of the device or a previous get_capabilities query.
        @param channels A channels list [chan_arg, ..., chan_arg].
        @param command A command list [command, arg, ..., argN]).
        @param transaction_id A transaction ID uuid4 or string 'create,'
            or 'none.'
        @retval A reply dict
            {'success': success,
            'result': {chan_arg: (success, command_specific_values),
            ..., chan_arg: (success, command_specific_values)},
            'transaction_id': transaction_id}.
        """
        assert(isinstance(channels, list)), 'Expected a channels list.'
        assert(isinstance(command, list)), 'Expected a command list.'
        assert(isinstance(transaction_id, str)), \
            'Expected a transaction_id str.'

        yield self._check_init()
        content = {'channels': channels, 'command': command,
                   'transaction_id': transaction_id}
        (content, headers, messaage) = yield \
            self.rpc_send('execute_device', content,
                          timeout=self.default_rpc_timeout)

        assert(isinstance(content, dict))
        defer.returnValue(content)

    @defer.inlineCallbacks
    def get_device(self, params, transaction_id='none'):
        """
        Get configuration parameters from the instrument.
        @param params A parameters list [(chan_arg, param_arg), ...,
            (chan_arg, param_arg)].
        @param transaction_id A transaction ID uuid4 or string 'create,'
            or 'none.'
        @retval A reply dict
            {'success': success,
            'result': {(chan_arg, param_arg): (success, val),
            ..., (chan_arg, param_arg): (success, val)},
            'transaction_id': transaction_id}
        """

        assert(isinstance(params, list)), 'Expected a parameter list.'
        assert(isinstance(transaction_id, str)), \
            'Expected a transaction_id str.'

        yield self._check_init()
        content = {'params': params, 'transaction_id': transaction_id}
        (content, headers, messaage) = yield \
            self.rpc_send('get_device', content,
                          timeout=self.default_rpc_timeout)

        assert(isinstance(content, dict))
        defer.returnValue(content)

    @defer.inlineCallbacks
    def set_device(self, params, transaction_id='none'):
        """
        Set parameters to the instrument side of of the agent.
        @param params A parameter-value dict {(chan_arg, param_arg): val,
        ..., (chan_arg, param_arg): val}.
        @param transaction_id A transaction ID uuid4 or string 'create,'
            or 'none.'
        @retval Reply dict
            {'success': success, 'result': {(chan_arg, param_arg): success,
            ..., chan_arg, param_arg): success},
            'transaction_id': transaction_id}.
        """
        assert(isinstance(params, dict)), 'Expected a parameter-value dict.'
        assert(isinstance(transaction_id, str)), \
            'Expected a transaction_id str.'

        yield self._check_init()
        content = {'params': params, 'transaction_id': transaction_id}
        (content, headers, messaage) = yield \
            self.rpc_send('set_device', content,
                          timeout=self.default_rpc_timeout)

        assert(isinstance(content, dict))
        defer.returnValue(content)

    @defer.inlineCallbacks
    def get_device_metadata(self, params, transaction_id='none'):
        """
        Retrieve metadata for the device, its transducers and parameters.
        @param params A metadata parameter list
            [(chan_arg, param_arg, meta_arg),
                ..., (chan_arg, param_arg, meta_arg)].
        @param transaction_id A transaction ID uuid4 or string 'create,'
            or 'none.'
        @retval Reply dict
            {'success': success,
            'result': {(chan_arg, param_arg, meta_arg): (success, val), ...,
                (chan_arg, param_arg, meta_arg): (success, val)},
            'transaction_id': transaction_id}.
        """

        assert(isinstance(params, list)), 'Expected a parameter list.'
        assert(isinstance(transaction_id, str)), \
            'Expected a transaction_id str.'

        yield self._check_init()
        content = {'params': params, 'transaction_id': transaction_id}
        (content, headers, messaage) = yield \
            self.rpc_send('get_device_metadata', content,
                          timeout=self.default_rpc_timeout)

        assert(isinstance(content, dict))
        defer.returnValue(content)

    @defer.inlineCallbacks
    def get_device_status(self, params, transaction_id='none'):
        """
        Obtain the status of an instrument. This includes non-parameter
        and non-lifecycle state of the instrument.
        @param params A parameter list [(chan_arg, status_arg), ...,
            (chan_arg, status_arg)].
        @param transaction_id A transaction ID uuid4 or string 'create,'
            or 'none.'
        @retval A reply dict
            {'success': success,
            'result': {(chan_arg, status_arg): (success, val), ...,
                (chan_arg, status_arg): (success, val)},
            'transaction_id': transaction_id}.
        """

        assert(isinstance(params, list)), 'Expected a parameter list.'
        assert(isinstance(transaction_id, str)), \
            'Expected a transaction_id str.'

        yield self._check_init()
        content = {'params': params, 'transaction_id': transaction_id}
        (content, headers, messaage) = yield \
            self.rpc_send('get_device_status', content,
                          timeout=self.default_rpc_timeout)

        assert(isinstance(content, dict))
        defer.returnValue(content)

    @defer.inlineCallbacks
    def execute_device_direct(self, bytes, transaction_id='none'):
        """
        Execute untranslated byte data commands on the device.
        Must be in direct access mode and possess the correct
        transaction_id key for the direct access session.
        @param bytes An untranslated block of data to send to the device.
        @param transaction_id A transaction ID uuid4 specifying the
        direct access session.
        @retval A reply dict {'success': success, 'result': bytes}.
        """

        assert(bytes), 'Expected command bytes.'
        assert(isinstance(transaction_id, str)), \
            'Expected a transaction_id str.'

        yield self._check_init()
        content = {'bytes': bytes, 'transaction_id': transaction_id}
        (content, headers, messaage) = yield \
            self.rpc_send('execute_device_direct', content,
                          timeout=self.default_rpc_timeout)

        assert(isinstance(content, dict))
        defer.returnValue(content)

    ###########################################################################
    #   Publishing interface.
    ###########################################################################

    # op_publish and op_driver_event_occurred are used by the driver
    # child process and are not invoked through a client.

    ###########################################################################
    #   Registration interface.
    ###########################################################################

    @defer.inlineCallbacks
    def register_resource(self, instrument_id):
        """
        Register the resource. Since this is a subclass, make the appropriate
        resource description for the registry and pass that into the
        registration call.
        """

        """
        ia_instance = InstrumentAgentResourceInstance()
        ci_params = yield self.get_observatory([driver_address])
        ia_instance.driver_process_id = ci_params[driver_address]
        ia_instance.instrument_ref = ResourceReference(
            RegistryIdentity=instrument_id, RegistryBranch='master')
        result = yield ResourceAgentClient.register_resource(self,
                                                             ia_instance)
        defer.returnValue(result)
        """
        pass

# Spawn of the process using the module name
factory = ProcessFactory(InstrumentAgent)

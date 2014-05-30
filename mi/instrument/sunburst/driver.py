"""
@package mi.instrument.sunburst.driver
@file marine-integrations/mi/instrument/sunburst/driver.py
@author Stuart Pearce, Chris Wingard & Kevin Stiemke
@brief Base Driver for the SAMI instruments
Release notes:
    Sunburst Instruments SAMI2-PCO2 partial CO2 & SAMI2-PH pH underwater
    sensors.

    This is the base driver that contains common code for the SAMI2
    instruments SAMI2-PCO2 & SAMI2-PH since they have the same basic
    SAMI2 operating structure.

    Some of this code also derives from initial code developed by Chris
    Center
"""

__author__ = 'Chris Wingard, Stuart Pearce & Kevin Stiemke'
__license__ = 'Apache 2.0'

## TODO: Make all commands executable in the autosample state
## TODO: Remove buffering all commands except status, maybe stop autosample
## TODO: Add timing tests for all functionality
## TODO: Test that no samples are received when autosample stopped
## TODO: Add async timeout exceptions

import re
import time
import datetime

from mi.core.log import get_logger
log = get_logger()

from mi.core.exceptions import SampleException
from mi.core.exceptions import InstrumentProtocolException
from mi.core.exceptions import InstrumentParameterException
from mi.core.exceptions import InstrumentTimeoutException

from mi.core.driver_scheduler import \
    DriverSchedulerConfigKey, \
    TriggerType

from mi.core.util import dict_equal
from mi.core.common import BaseEnum
from mi.core.instrument.chunker import StringChunker
from mi.core.instrument.data_particle import DataParticle
from mi.core.instrument.data_particle import DataParticleKey
from mi.core.instrument.data_particle import CommonDataParticleType
from mi.core.instrument.instrument_fsm import InstrumentFSM
from mi.core.instrument.instrument_driver import SingleConnectionInstrumentDriver
from mi.core.instrument.instrument_driver import DriverEvent
from mi.core.instrument.instrument_driver import DriverAsyncEvent
from mi.core.instrument.instrument_driver import DriverProtocolState
from mi.core.instrument.instrument_driver import DriverParameter
from mi.core.instrument.instrument_driver import ResourceAgentState
from mi.core.instrument.instrument_driver import DriverConfigKey
from mi.core.instrument.instrument_protocol import CommandResponseInstrumentProtocol
from mi.core.instrument.driver_dict import DriverDictKey
from mi.core.instrument.protocol_param_dict import ParameterDictType
from mi.core.instrument.protocol_param_dict import ProtocolParameterDict
from mi.core.instrument.protocol_param_dict import ParameterDictVisibility
from mi.core.instrument.protocol_param_dict import FunctionParameter

from mi.core.exceptions import InstrumentProtocolException
from mi.core.exceptions import InstrumentParameterException
from mi.core.exceptions import NotImplementedException
from mi.core.exceptions import SampleException

from mi.core.time import get_timestamp
from mi.core.time import get_timestamp_delayed

###
#    Driver Constant Definitions
###
# newline.
NEWLINE = '\r'

# default command timeout.
TIMEOUT = 10

UNIX_EPOCH = datetime.datetime(1970, 1, 1)
SAMI_EPOCH = datetime.datetime(1904, 1, 1)
SAMI_UNIX_OFFSET = UNIX_EPOCH - SAMI_EPOCH
FIVE_YEARS_IN_SECONDS = 0x0968A480
ONE_YEAR_IN_SECONDS = 0x01E13380

## Time delay between retrieving system time and setting SAMI time.  Multiple commands are sent before the time.
##   Each command has a wakeup which takes 1 second.
TIME_WAKEUP_DELAY = 8

WAKEUP_DELAY = 0.5 # Time between sending newlines to wakeup SAMI

# Length of configuration string with '0' padding
# used to calculate number of '0' padding
CONFIG_WITH_0_PADDING = 232

# Length of configuration string with 'f' padding
# used to calculate number of 'f' padding
CONFIG_WITH_0_AND_F_PADDING = 512

# Terminator at the end of a configuration string
CONFIG_TERMINATOR = '00'

# Pump on, valve off
PUMP_REAGENT = '01'
# Pump on, valve on
PUMP_DEIONIZED_WATER = '03'
# 1/8 second
PUMP_DURATION_UNITS = 0.125
# 1/8 second increments to pump 50ml
PUMP_DURATION_50ML = 8
# Sleep time between 50ml pumps
PUMP_SLEEP_50ML = 2.0
# Value added to pump duration for timeout
PUMP_TIMEOUT_OFFSET = 5.0

###
#    Driver RegEx Definitions
###

# Regular Status Strings (produced every 1 Hz or in response to S0 command)
REGULAR_STATUS_REGEX = (
    r'[:]' +  # status message identifier
    '([0-9A-Fa-f]{8})' +  # timestamp (seconds since 1904)
    '([0-9A-Fa-f]{4})' +  # status bit field
    '([0-9A-Fa-f]{6})' +  # number of data records recorded
    '([0-9A-Fa-f]{6})' +  # number of errors
    '([0-9A-Fa-f]{6})' +  # number of bytes stored
    '([0-9A-Fa-f]{2})' +  # unique id
    NEWLINE)
REGULAR_STATUS_REGEX_MATCHER = re.compile(REGULAR_STATUS_REGEX)

# Control Records (Types 0x80 - 0xFF)
CONTROL_RECORD_REGEX = (
    r'[\*]' +  # record identifier
    '([0-9A-Fa-f]{2})' +  # unique instrument identifier
    '([0-9A-Fa-f]{2})' +  # control record length (bytes)
    '([8-9A-Fa-f][0-9A-Fa-f])' +  # type of control record 0x80-FF
    '([0-9A-Fa-f]{8})' +  # timestamp (seconds since 1904)
    '([0-9A-Fa-f]{4})' +  # status bit field
    '([0-9A-Fa-f]{6})' +  # number of data records recorded
    '([0-9A-Fa-f]{6})' +  # number of errors
    '([0-9A-Fa-f]{6})' +  # number of bytes stored
    '([0-9A-Fa-f]{2})' +  # checksum
    NEWLINE)
CONTROL_RECORD_REGEX_MATCHER = re.compile(CONTROL_RECORD_REGEX)

BATTERY_VOLTAGE_REGEX = (
    r'([0-9A-Fa-f]{4})' +
    NEWLINE)
BATTERY_VOLTAGE_REGEX_MATCHER = re.compile(BATTERY_VOLTAGE_REGEX)

THERMISTOR_VOLTAGE_REGEX = (
    r'([0-9A-Fa-f]{4})' +
    NEWLINE)
THERMISTOR_VOLTAGE_REGEX_MATCHER = re.compile(BATTERY_VOLTAGE_REGEX)

# Error records
ERROR_REGEX = r'[\?]([0-9A-Fa-f]{2})' + NEWLINE
ERROR_REGEX_MATCHER = re.compile(ERROR_REGEX)

## These are returned immediately after SAMI sample commands
BLANK_SAMPLE_RETURN_REGEX = (r'\^05')
BLANK_SAMPLE_RETURN_REGEX_MATCHER = re.compile(BLANK_SAMPLE_RETURN_REGEX)
SAMPLE_RETURN_REGEX = (r'\^04')
SAMPLE_RETURN_REGEX_MATCHER = re.compile(SAMPLE_RETURN_REGEX)

# Currently used to handle unexpected responses
WILD_CARD_REGEX  = r'.*' + NEWLINE
WILD_CARD_REGEX_MATCHER = re.compile(WILD_CARD_REGEX)

NEW_LINE_REGEX = NEWLINE
NEW_LINE_REGEX_MATCHER = re.compile(NEW_LINE_REGEX)

###
#    Begin Classes
###

class SamiScheduledJob(BaseEnum):
    AUTO_SAMPLE = 'auto_sample'
    ACQUIRE_STATUS = 'acquire_status'

class SamiDataParticleType(BaseEnum):
    """
    Base class Data particle types produced by a SAMI instrument. Should be
    sub-classed in the specific instrument driver
    """

    RAW = CommonDataParticleType.RAW
    REGULAR_STATUS = 'pco2w_regular_status'
    CONTROL_RECORD = 'pco2w_control_record'
    BATTERY_VOLTAGE = 'pco2w_battery_voltage'
    THERMISTOR_VOLTAGE = 'pco2w_thermistor_voltage'

class SamiProtocolState(BaseEnum):
    """
    Instrument protocol states
    """

    UNKNOWN = DriverProtocolState.UNKNOWN
    WAITING = 'PROTOCOL_STATE_WAITING'
    COMMAND = DriverProtocolState.COMMAND
    AUTOSAMPLE = DriverProtocolState.AUTOSAMPLE
    DIRECT_ACCESS = DriverProtocolState.DIRECT_ACCESS
    POLLED_SAMPLE = 'PROTOCOL_STATE_POLLED_SAMPLE'
    SCHEDULED_SAMPLE = 'PROTOCOL_STATE_SCHEDULED_SAMPLE'
    DEIONIZED_WATER_FLUSH = 'PROTOCOL_STATE_DEIONIZED_WATER_FLUSH'
    REAGENT_FLUSH = 'PROTOCOL_STATE_REAGENT_FLUSH'
    DEIONIZED_WATER_FLUSH_100ML = 'PROTOCOL_STATE_DEIONIZED_WATER_FLUSH_100ML'
    REAGENT_FLUSH_100ML = 'PROTOCOL_STATE_REAGENT_FLUSH_100ML'

class SamiProtocolEvent(BaseEnum):
    """
    Protocol events
    """

    ENTER = DriverEvent.ENTER
    EXIT = DriverEvent.EXIT
    GET = DriverEvent.GET
    SET = DriverEvent.SET
    DISCOVER = DriverEvent.DISCOVER
    START_AUTOSAMPLE = DriverEvent.START_AUTOSAMPLE
    STOP_AUTOSAMPLE = DriverEvent.STOP_AUTOSAMPLE
    EXECUTE_DIRECT = DriverEvent.EXECUTE_DIRECT
    START_DIRECT = DriverEvent.START_DIRECT
    STOP_DIRECT = DriverEvent.STOP_DIRECT
    ACQUIRE_SAMPLE = DriverEvent.ACQUIRE_SAMPLE
    ACQUIRE_STATUS = DriverEvent.ACQUIRE_STATUS

    TAKE_SAMPLE = 'PROTOCOL_EVENT_TAKE_SAMPLE'
    SUCCESS = 'PROTOCOL_EVENT_SUCCESS'  # success getting a sample
    TIMEOUT = 'PROTOCOL_EVENT_TIMEOUT'  # timeout while getting a sample

    DEIONIZED_WATER_FLUSH = 'DRIVER_EVENT_DEIONIZED_WATER_FLUSH'
    REAGENT_FLUSH = 'DRIVER_EVENT_REAGENT_FLUSH'
    DEIONIZED_WATER_FLUSH_100ML = 'DRIVER_EVENT_DEIONIZED_WATER_FLUSH_100ML'
    REAGENT_FLUSH_100ML = 'DRIVER_EVENT_REAGENT_FLUSH_100ML'

    EXECUTE_FLUSH = 'PROTOCOL_EVENT_EXECUTE_FLUSH'

class SamiCapability(BaseEnum):
    """
    Protocol events that should be exposed to users (subset of above).
    """

    ACQUIRE_STATUS = SamiProtocolEvent.ACQUIRE_STATUS
    ACQUIRE_SAMPLE = SamiProtocolEvent.ACQUIRE_SAMPLE
    START_AUTOSAMPLE = SamiProtocolEvent.START_AUTOSAMPLE
    STOP_AUTOSAMPLE = SamiProtocolEvent.STOP_AUTOSAMPLE

    DEIONIZED_WATER_FLUSH = SamiProtocolEvent.DEIONIZED_WATER_FLUSH
    REAGENT_FLUSH = SamiProtocolEvent.REAGENT_FLUSH
    DEIONIZED_WATER_FLUSH_100ML = SamiProtocolEvent.DEIONIZED_WATER_FLUSH_100ML
    REAGENT_FLUSH_100ML = SamiProtocolEvent.REAGENT_FLUSH_100ML

class SamiParameter(DriverParameter):
    """
    Base SAMI instrument parameters. Subclass and extend this Enum with device
    specific parameters in subclass 'Parameter'.
    """

    LAUNCH_TIME = 'launch_time'
    START_TIME_FROM_LAUNCH = 'start_time_from_launch'
    STOP_TIME_FROM_START = 'stop_time_from_start'
    MODE_BITS = 'mode_bits'
    SAMI_SAMPLE_INTERVAL = 'sami_sample_interval'
    SAMI_DRIVER_VERSION = 'sami_driver_version'
    SAMI_PARAMS_POINTER = 'sami_params_pointer'
    DEVICE1_SAMPLE_INTERVAL = 'device1_sample_interval'
    DEVICE1_DRIVER_VERSION = 'device1_driver_version'
    DEVICE1_PARAMS_POINTER = 'device1_params_pointer'
    DEVICE2_SAMPLE_INTERVAL = 'device2_sample_interval'
    DEVICE2_DRIVER_VERSION = 'device2_driver_version'
    DEVICE2_PARAMS_POINTER = 'device2_params_pointer'
    DEVICE3_SAMPLE_INTERVAL = 'device3_sample_interval'
    DEVICE3_DRIVER_VERSION = 'device3_driver_version'
    DEVICE3_PARAMS_POINTER = 'device3_params_pointer'
    PRESTART_SAMPLE_INTERVAL = 'prestart_sample_interval'
    PRESTART_DRIVER_VERSION = 'prestart_driver_version'
    PRESTART_PARAMS_POINTER = 'prestart_params_pointer'
    GLOBAL_CONFIGURATION = 'global_configuration'
    AUTO_SAMPLE_INTERVAL = 'auto_sample_interval'
    FLUSH_DURATION = 'flush_duration'
    PUMP_100ML_CYCLES = 'pump_100ml_cycles'

    # make sure to extend these in the individual drivers with the
    # the portions of the configuration that is unique to each.

class Prompt(BaseEnum):
    """
    Device i/o prompts..
    """

    # The boot prompt is the prompt of the SAMI2's Lower Level operating
    # system. If this prompt is reached, it means the SAMI2 instrument
    # software has crashed and needs to be restarted with the command
    # 'u'. If this has occurred, the instrument has been reset and will
    # be in an unconfigured state.
    BOOT_PROMPT = '7.7Boot>'

    # No true prompts
    # COMMAND = 'None'

class SamiInstrumentCommand(BaseEnum):
    """
    Base SAMI instrument command strings. Subclass and extend these with device
    specific commands in subclass 'InstrumentCommand'.

    This applies to the PCO2 where an additional ACQUIRE_SAMPLE
    command is required for device 1, the external pump.
    """

    GET_STATUS = 'S0'
    START_STATUS = 'F0'
    STOP_STATUS = 'F5A'
    GET_CONFIG = 'L'
    SET_CONFIG = 'L5A'
    ERASE_ALL = 'E5A'
    GET_BATTERY_VOLTAGE = 'B'
    GET_THERMISTOR_VOLTAGE = 'T'

    START = 'G5A'
    STOP = 'Q5A'

    ACQUIRE_SAMPLE_SAMI = 'R'
    ESCAPE_BOOT = 'u'

    PUMP_DEIONIZED_WATER_SAMI = 'P' + PUMP_DEIONIZED_WATER
    PUMP_REAGENT_SAMI = 'P' + PUMP_REAGENT
    PUMP_OFF = 'P'

###############################################################################
# Data Particles
###############################################################################

class SamiBatteryVoltageDataParticleKey(BaseEnum):

    BATTERY_VOLTAGE = 'pco2w_battery_voltage'

class SamiBatteryVoltageDataParticle(DataParticle):
    """
    Routines for parsing raw data into an regular status data particle
    structure.
    @throw SampleException If there is a problem with sample creation
    """

    _data_particle_type = SamiDataParticleType.BATTERY_VOLTAGE

    def _build_parsed_values(self):

        matched = BATTERY_VOLTAGE_REGEX_MATCHER.match(self.raw_data)
        if not matched:
            raise SampleException("No regex match of parsed sample data: [%s]" %
                                  self.decoded_raw)

        result = [{DataParticleKey.VALUE_ID: SamiBatteryVoltageDataParticleKey.BATTERY_VOLTAGE,
                   DataParticleKey.VALUE: int(matched.group(1), 16)}]

        return result

class SamiThermistorVoltageDataParticleKey(BaseEnum):

    THERMISTOR_VOLTAGE = 'pco2w_thermistor_voltage'

class SamiThermistorVoltageDataParticle(DataParticle):
    """
    Routines for parsing raw data into an regular status data particle
    structure.
    @throw SampleException If there is a problem with sample creation
    """
    _data_particle_type = SamiDataParticleType.THERMISTOR_VOLTAGE

    def _build_parsed_values(self):

        matched = THERMISTOR_VOLTAGE_REGEX_MATCHER.match(self.raw_data)
        if not matched:
            raise SampleException("No regex match of parsed sample data: [%s]" %
                                  self.decoded_raw)

        result = [{DataParticleKey.VALUE_ID: SamiThermistorVoltageDataParticleKey.THERMISTOR_VOLTAGE,
                   DataParticleKey.VALUE: int(matched.group(1), 16)}]

        log.debug('SamiProtocol.SamiThermistorVoltageDataParticle(): result = ' + str(result))

        return result

class SamiRegularStatusDataParticleKey(BaseEnum):
    """
    Data particle key for the regular (1 Hz or regular) status messages.
    """

    ELAPSED_TIME_CONFIG = "elapsed_time_config"
    CLOCK_ACTIVE = 'clock_active'
    RECORDING_ACTIVE = 'recording_active'
    RECORD_END_ON_TIME = 'record_end_on_time'
    RECORD_MEMORY_FULL = 'record_memory_full'
    RECORD_END_ON_ERROR = 'record_end_on_error'
    DATA_DOWNLOAD_OK = 'data_download_ok'
    FLASH_MEMORY_OPEN = 'flash_memory_open'
    BATTERY_LOW_PRESTART = 'battery_low_prestart'
    BATTERY_LOW_MEASUREMENT = 'battery_low_measurement'
    BATTERY_LOW_BANK = 'battery_low_bank'
    BATTERY_LOW_EXTERNAL = 'battery_low_external'
    EXTERNAL_DEVICE1_FAULT = 'external_device1_fault'
    EXTERNAL_DEVICE2_FAULT = 'external_device2_fault'
    EXTERNAL_DEVICE3_FAULT = 'external_device3_fault'
    FLASH_ERASED = 'flash_erased'
    POWER_ON_INVALID = 'power_on_invalid'
    NUM_DATA_RECORDS = 'num_data_records'
    NUM_ERROR_RECORDS = 'num_error_records'
    NUM_BYTES_STORED = 'num_bytes_stored'
    UNIQUE_ID = 'unique_id'


class SamiRegularStatusDataParticle(DataParticle):
    """
    Routines for parsing raw data into an regular status data particle
    structure.
    @throw SampleException If there is a problem with sample creation
    """

    _data_particle_type = SamiDataParticleType.REGULAR_STATUS

    def _build_parsed_values(self):
        """
        Parse regular status values from raw data into a dictionary
        """

        ### Regular Status Messages
        # Produced in response to S0 command, or automatically at 1 Hz. All
        # regular status messages are preceeded by the ':' character and
        # terminate with a '/r'. Sample string:
        #
        #   :CEE90B1B004100000100000000021254
        #
        # These messages consist of the time since the last configuration,
        # status flags, the number of data records, the number of error
        # records, the number of bytes stored (including configuration bytes),
        # and the instrument's unique id.
        ###

        matched = REGULAR_STATUS_REGEX_MATCHER.match(self.raw_data)
        if not matched:
            raise SampleException("No regex match of parsed sample data: [%s]" %
                                  self.decoded_raw)

        particle_keys = [SamiRegularStatusDataParticleKey.ELAPSED_TIME_CONFIG,
                         SamiRegularStatusDataParticleKey.CLOCK_ACTIVE,
                         SamiRegularStatusDataParticleKey.RECORDING_ACTIVE,
                         SamiRegularStatusDataParticleKey.RECORD_END_ON_TIME,
                         SamiRegularStatusDataParticleKey.RECORD_MEMORY_FULL,
                         SamiRegularStatusDataParticleKey.RECORD_END_ON_ERROR,
                         SamiRegularStatusDataParticleKey.DATA_DOWNLOAD_OK,
                         SamiRegularStatusDataParticleKey.FLASH_MEMORY_OPEN,
                         SamiRegularStatusDataParticleKey.BATTERY_LOW_PRESTART,
                         SamiRegularStatusDataParticleKey.BATTERY_LOW_MEASUREMENT,
                         SamiRegularStatusDataParticleKey.BATTERY_LOW_BANK,
                         SamiRegularStatusDataParticleKey.BATTERY_LOW_EXTERNAL,
                         SamiRegularStatusDataParticleKey.EXTERNAL_DEVICE1_FAULT,
                         SamiRegularStatusDataParticleKey.EXTERNAL_DEVICE2_FAULT,
                         SamiRegularStatusDataParticleKey.EXTERNAL_DEVICE3_FAULT,
                         SamiRegularStatusDataParticleKey.FLASH_ERASED,
                         SamiRegularStatusDataParticleKey.POWER_ON_INVALID,
                         SamiRegularStatusDataParticleKey.NUM_DATA_RECORDS,
                         SamiRegularStatusDataParticleKey.NUM_ERROR_RECORDS,
                         SamiRegularStatusDataParticleKey.NUM_BYTES_STORED,
                         SamiRegularStatusDataParticleKey.UNIQUE_ID]

        result = []
        grp_index = 1  # used to index through match groups, starting at 1
        bit_index = 0  # used to index through the bit fields represented by
                       # the two bytes after CLOCK_ACTIVE.

        for key in particle_keys:
            if key in [SamiRegularStatusDataParticleKey.CLOCK_ACTIVE,
                       SamiRegularStatusDataParticleKey.RECORDING_ACTIVE,
                       SamiRegularStatusDataParticleKey.RECORD_END_ON_TIME,
                       SamiRegularStatusDataParticleKey.RECORD_MEMORY_FULL,
                       SamiRegularStatusDataParticleKey.RECORD_END_ON_ERROR,
                       SamiRegularStatusDataParticleKey.DATA_DOWNLOAD_OK,
                       SamiRegularStatusDataParticleKey.FLASH_MEMORY_OPEN,
                       SamiRegularStatusDataParticleKey.BATTERY_LOW_PRESTART,
                       SamiRegularStatusDataParticleKey.BATTERY_LOW_MEASUREMENT,
                       SamiRegularStatusDataParticleKey.BATTERY_LOW_BANK,
                       SamiRegularStatusDataParticleKey.BATTERY_LOW_EXTERNAL,
                       SamiRegularStatusDataParticleKey.EXTERNAL_DEVICE1_FAULT,
                       SamiRegularStatusDataParticleKey.EXTERNAL_DEVICE2_FAULT,
                       SamiRegularStatusDataParticleKey.EXTERNAL_DEVICE3_FAULT,
                       SamiRegularStatusDataParticleKey.FLASH_ERASED,
                       SamiRegularStatusDataParticleKey.POWER_ON_INVALID]:
                # if the keys match values represented by the bits in the two
                # byte status flags value, parse bit-by-bit using the bit-shift
                # operator to determine the boolean value.
                result.append({DataParticleKey.VALUE_ID: key,
                               DataParticleKey.VALUE: bool(int(matched.group(2), 16) & (1 << bit_index))})
                bit_index += 1  # bump the bit index
                grp_index = 3  # set the right group index for when we leave this part of the loop.
            else:
                # otherwise all values in the string are parsed to integers
                result.append({DataParticleKey.VALUE_ID: key,
                               DataParticleKey.VALUE: int(matched.group(grp_index), 16)})
                grp_index += 1

        return result

class SamiControlRecordDataParticleKey(BaseEnum):
    """
    Data particle key for peridoically produced control records.
    """

    UNIQUE_ID = 'unique_id'
    RECORD_LENGTH = 'record_length'
    RECORD_TYPE = 'record_type'
    RECORD_TIME = 'record_time'
    CLOCK_ACTIVE = 'clock_active'
    RECORDING_ACTIVE = 'recording_active'
    RECORD_END_ON_TIME = 'record_end_on_time'
    RECORD_MEMORY_FULL = 'record_memory_full'
    RECORD_END_ON_ERROR = 'record_end_on_error'
    DATA_DOWNLOAD_OK = 'data_download_ok'
    FLASH_MEMORY_OPEN = 'flash_memory_open'
    BATTERY_LOW_PRESTART = 'battery_low_prestart'
    BATTERY_LOW_MEASUREMENT = 'battery_low_measurement'
    BATTERY_LOW_BANK = 'battery_low_bank'
    BATTERY_LOW_EXTERNAL = 'battery_low_external'
    EXTERNAL_DEVICE1_FAULT = 'external_device1_fault'
    EXTERNAL_DEVICE2_FAULT = 'external_device2_fault'
    EXTERNAL_DEVICE3_FAULT = 'external_device3_fault'
    FLASH_ERASED = 'flash_erased'
    POWER_ON_INVALID = 'power_on_invalid'
    NUM_DATA_RECORDS = 'num_data_records'
    NUM_ERROR_RECORDS = 'num_error_records'
    NUM_BYTES_STORED = 'num_bytes_stored'
    CHECKSUM = 'checksum'

class SamiControlRecordDataParticle(DataParticle):
    """
    Routines for parsing raw data into a control record data particle
    structure.
    @throw SampleException If there is a problem with sample creation
    """

    _data_particle_type = SamiDataParticleType.CONTROL_RECORD

    def _build_parsed_values(self):
        """
        Parse control record values from raw data into a dictionary
        """

        ### Control Records
        # Produced by the instrument periodically in reponse to certain events
        # (e.g. when the Flash memory is opened). The messages are preceded by
        # a '*' character and terminated with a '\r'. Sample string:
        #
        #   *541280CEE90B170041000001000000000200AF
        #
        # A full description of the control record strings can be found in the
        # vendor supplied SAMI Record Format document.
        ###

        matched = CONTROL_RECORD_REGEX_MATCHER.match(self.raw_data)
        if not matched:
            raise SampleException("No regex match of parsed sample data: [%s]" %
                                  self.decoded_raw)

        particle_keys = [SamiControlRecordDataParticleKey.UNIQUE_ID,
                         SamiControlRecordDataParticleKey.RECORD_LENGTH,
                         SamiControlRecordDataParticleKey.RECORD_TYPE,
                         SamiControlRecordDataParticleKey.RECORD_TIME,
                         SamiControlRecordDataParticleKey.CLOCK_ACTIVE,
                         SamiControlRecordDataParticleKey.RECORDING_ACTIVE,
                         SamiControlRecordDataParticleKey.RECORD_END_ON_TIME,
                         SamiControlRecordDataParticleKey.RECORD_MEMORY_FULL,
                         SamiControlRecordDataParticleKey.RECORD_END_ON_ERROR,
                         SamiControlRecordDataParticleKey.DATA_DOWNLOAD_OK,
                         SamiControlRecordDataParticleKey.FLASH_MEMORY_OPEN,
                         SamiControlRecordDataParticleKey.BATTERY_LOW_PRESTART,
                         SamiControlRecordDataParticleKey.BATTERY_LOW_MEASUREMENT,
                         SamiControlRecordDataParticleKey.BATTERY_LOW_BANK,
                         SamiControlRecordDataParticleKey.BATTERY_LOW_EXTERNAL,
                         SamiControlRecordDataParticleKey.EXTERNAL_DEVICE1_FAULT,
                         SamiControlRecordDataParticleKey.EXTERNAL_DEVICE2_FAULT,
                         SamiControlRecordDataParticleKey.EXTERNAL_DEVICE3_FAULT,
                         SamiControlRecordDataParticleKey.FLASH_ERASED,
                         SamiControlRecordDataParticleKey.POWER_ON_INVALID,
                         SamiControlRecordDataParticleKey.NUM_DATA_RECORDS,
                         SamiControlRecordDataParticleKey.NUM_ERROR_RECORDS,
                         SamiControlRecordDataParticleKey.NUM_BYTES_STORED,
                         SamiControlRecordDataParticleKey.CHECKSUM]

        result = []
        grp_index = 1  # used to index through match groups, starting at 1/
        bit_index = 0  # used to index through the bit fields represented by
                       # the two bytes after CLOCK_ACTIVE.

        for key in particle_keys:
            if key in [SamiControlRecordDataParticleKey.CLOCK_ACTIVE,
                       SamiControlRecordDataParticleKey.RECORDING_ACTIVE,
                       SamiControlRecordDataParticleKey.RECORD_END_ON_TIME,
                       SamiControlRecordDataParticleKey.RECORD_MEMORY_FULL,
                       SamiControlRecordDataParticleKey.RECORD_END_ON_ERROR,
                       SamiControlRecordDataParticleKey.DATA_DOWNLOAD_OK,
                       SamiControlRecordDataParticleKey.FLASH_MEMORY_OPEN,
                       SamiControlRecordDataParticleKey.BATTERY_LOW_PRESTART,
                       SamiControlRecordDataParticleKey.BATTERY_LOW_MEASUREMENT,
                       SamiControlRecordDataParticleKey.BATTERY_LOW_BANK,
                       SamiControlRecordDataParticleKey.BATTERY_LOW_EXTERNAL,
                       SamiControlRecordDataParticleKey.EXTERNAL_DEVICE1_FAULT,
                       SamiControlRecordDataParticleKey.EXTERNAL_DEVICE2_FAULT,
                       SamiControlRecordDataParticleKey.EXTERNAL_DEVICE3_FAULT,
                       SamiControlRecordDataParticleKey.FLASH_ERASED,
                       SamiControlRecordDataParticleKey.POWER_ON_INVALID]:
                # if the keys match values represented by the bits in the two
                # byte status flags value included in all control records,
                # parse bit-by-bit using the bit-shift operator to determine
                # boolean value.
                result.append({DataParticleKey.VALUE_ID: key,
                               DataParticleKey.VALUE: bool(int(matched.group(5), 16) & (1 << bit_index))})
                bit_index += 1  # bump the bit index
                grp_index = 6  # set the right group index for when we leave this part of the loop.
            else:
                # otherwise all values in the string are parsed to integers
                result.append({DataParticleKey.VALUE_ID: key,
                               DataParticleKey.VALUE: int(matched.group(grp_index), 16)})
                grp_index += 1

        return result


class SamiConfigDataParticleKey(BaseEnum):
    """
    SAMI Instrument Data particle key Base Class for configuration records.
    This should be subclassed in the specific instrument driver and extended
    with specific instrument parameters.
    """

    LAUNCH_TIME = 'launch_time'
    START_TIME_OFFSET = 'start_time_offset'
    RECORDING_TIME = 'recording_time'
    PMI_SAMPLE_SCHEDULE = 'pmi_sample_schedule'
    SAMI_SAMPLE_SCHEDULE = 'sami_sample_schedule'
    SLOT1_FOLLOWS_SAMI_SCHEDULE = 'slot1_follows_sami_sample'
    SLOT1_INDEPENDENT_SCHEDULE = 'slot1_independent_schedule'
    SLOT2_FOLLOWS_SAMI_SCHEDULE = 'slot2_follows_sami_sample'
    SLOT2_INDEPENDENT_SCHEDULE = 'slot2_independent_schedule'
    SLOT3_FOLLOWS_SAMI_SCHEDULE = 'slot3_follows_sami_sample'
    SLOT3_INDEPENDENT_SCHEDULE = 'slot3_independent_schedule'
    TIMER_INTERVAL_SAMI = 'timer_interval_sami'
    DRIVER_ID_SAMI = 'driver_id_sami'
    PARAMETER_POINTER_SAMI = 'parameter_pointer_sami'
    TIMER_INTERVAL_DEVICE1 = 'timer_interval_device1'
    DRIVER_ID_DEVICE1 = 'driver_id_device1'
    PARAMETER_POINTER_DEVICE1 = 'parameter_pointer_device1'
    TIMER_INTERVAL_DEVICE2 = 'timer_interval_device2'
    DRIVER_ID_DEVICE2 = 'driver_id_device2'
    PARAMETER_POINTER_DEVICE2 = 'parameter_pointer_device2'
    TIMER_INTERVAL_DEVICE3 = 'timer_interval_device3'
    DRIVER_ID_DEVICE3 = 'driver_id_device3'
    PARAMETER_POINTER_DEVICE3 = 'parameter_pointer_device3'
    TIMER_INTERVAL_PRESTART = 'timer_interval_prestart'
    DRIVER_ID_PRESTART = 'driver_id_prestart'
    PARAMETER_POINTER_PRESTART = 'parameter_pointer_prestart'
    USE_BAUD_RATE_57600 = 'use_baud_rate_57600'
    SEND_RECORD_TYPE = 'send_record_type'
    SEND_LIVE_RECORDS = 'send_live_records'
    EXTEND_GLOBAL_CONFIG = 'extend_global_config'
    # make sure to extend these in the individual drivers with the
    # the portions of the configuration that is unique to each.

class QueuedCommands():
    """
    Structure to buffer commands which are received when a sample is being taken
    """

    def __init__(self):
        """
        Initialize
        """
        self.sample = None  ## Can be None, ProtocolEvent.ACQUIRE_SAMPLE, or ProtocolEvent.ACQUIRE_BLANK_SAMPLE
        self.status = None  ## Can be None or ProtocolEvent.ACQUIRE_STATUS

    def reset(self):
        """
        Reset buffers
        """
        self.sample = None
        self.status = None

###############################################################################
# Driver
###############################################################################

class SamiInstrumentDriver(SingleConnectionInstrumentDriver):
    """
    SamiInstrumentDriver baseclass
    Subclasses SingleConnectionInstrumentDriver with connection state
    machine.

    Needs to be subclassed in the specific driver module.
    """

    def __init__(self, evt_callback):
        """
        Driver constructor.
        @param evt_callback Driver process event callback.
        """

        #Construct superclass.
        SingleConnectionInstrumentDriver.__init__(self, evt_callback)


###########################################################################
# Protocol
###########################################################################

class SamiProtocol(CommandResponseInstrumentProtocol):
    """
    SAMI Instrument protocol class
    Subclasses CommandResponseInstrumentProtocol

    Should be sub-classed in specific driver.
    """

    def __init__(self, prompts, newline, driver_event):
        """
        Protocol constructor.
        @param prompts A BaseEnum class containing instrument prompts.
        @param newline The newline.
        @param driver_event Driver process event callback.
        """
        log.debug('SamiProtocol.__init__()')

        # Add event handlers for protocol state machine
        self._protocol_fsm.add_handler(
            SamiProtocolState.UNKNOWN, SamiProtocolEvent.ENTER,
            self._handler_unknown_enter)
        self._protocol_fsm.add_handler(
            SamiProtocolState.UNKNOWN, SamiProtocolEvent.EXIT,
            self._handler_unknown_exit)
        self._protocol_fsm.add_handler(
            SamiProtocolState.UNKNOWN, SamiProtocolEvent.DISCOVER,
            self._handler_unknown_discover)

        self._protocol_fsm.add_handler(
            SamiProtocolState.WAITING, SamiProtocolEvent.ENTER,
            self._handler_waiting_enter)
        self._protocol_fsm.add_handler(
            SamiProtocolState.WAITING, SamiProtocolEvent.EXIT,
            self._handler_waiting_exit)
        self._protocol_fsm.add_handler(
            SamiProtocolState.WAITING, SamiProtocolEvent.DISCOVER,
            self._handler_waiting_discover)

        self._protocol_fsm.add_handler(
            SamiProtocolState.COMMAND, SamiProtocolEvent.ENTER,
            self._handler_command_enter)
        self._protocol_fsm.add_handler(
            SamiProtocolState.COMMAND, SamiProtocolEvent.EXIT,
            self._handler_command_exit)
        self._protocol_fsm.add_handler(
            SamiProtocolState.COMMAND, SamiProtocolEvent.GET,
            self._handler_command_get)
        self._protocol_fsm.add_handler(
            SamiProtocolState.COMMAND, SamiProtocolEvent.SET,
            self._handler_command_set)
        self._protocol_fsm.add_handler(
            SamiProtocolState.COMMAND, SamiProtocolEvent.START_DIRECT,
            self._handler_command_start_direct)
        self._protocol_fsm.add_handler(
            SamiProtocolState.COMMAND, SamiProtocolEvent.ACQUIRE_STATUS,
            self._handler_acquire_status)
        self._protocol_fsm.add_handler(
            SamiProtocolState.COMMAND, SamiProtocolEvent.ACQUIRE_SAMPLE,
            self._handler_command_acquire_sample)
        self._protocol_fsm.add_handler(
            SamiProtocolState.COMMAND, SamiProtocolEvent.START_AUTOSAMPLE,
            self._handler_command_start_autosample)
        self._protocol_fsm.add_handler(
            SamiProtocolState.COMMAND, SamiProtocolEvent.DEIONIZED_WATER_FLUSH,
            self._handler_command_deionized_water_flush)
        self._protocol_fsm.add_handler(
            SamiProtocolState.COMMAND, SamiProtocolEvent.REAGENT_FLUSH,
            self._handler_command_reagent_flush)
        self._protocol_fsm.add_handler(
            SamiProtocolState.COMMAND, SamiProtocolEvent.DEIONIZED_WATER_FLUSH_100ML,
            self._handler_command_deionized_water_flush_100ml)
        self._protocol_fsm.add_handler(
            SamiProtocolState.COMMAND, SamiProtocolEvent.REAGENT_FLUSH_100ML,
            self._handler_command_reagent_flush_100ml)

        self._protocol_fsm.add_handler(
            SamiProtocolState.DIRECT_ACCESS, SamiProtocolEvent.ENTER,
            self._handler_direct_access_enter)
        self._protocol_fsm.add_handler(
            SamiProtocolState.DIRECT_ACCESS, SamiProtocolEvent.EXIT,
            self._handler_direct_access_exit)
        self._protocol_fsm.add_handler(
            SamiProtocolState.DIRECT_ACCESS, SamiProtocolEvent.STOP_DIRECT,
            self._handler_direct_access_stop_direct)
        self._protocol_fsm.add_handler(
            SamiProtocolState.DIRECT_ACCESS, SamiProtocolEvent.EXECUTE_DIRECT,
            self._handler_direct_access_execute_direct)

        self._protocol_fsm.add_handler(
            SamiProtocolState.AUTOSAMPLE, SamiProtocolEvent.ENTER,
            self._handler_autosample_enter)
        self._protocol_fsm.add_handler(
            SamiProtocolState.AUTOSAMPLE, SamiProtocolEvent.EXIT,
            self._handler_autosample_exit)
        self._protocol_fsm.add_handler(
            SamiProtocolState.AUTOSAMPLE, SamiProtocolEvent.STOP_AUTOSAMPLE,
            self._handler_autosample_stop)
        self._protocol_fsm.add_handler(
            SamiProtocolState.AUTOSAMPLE, SamiProtocolEvent.ACQUIRE_STATUS,
            self._handler_acquire_status)
        self._protocol_fsm.add_handler(
            SamiProtocolState.AUTOSAMPLE, SamiProtocolEvent.ACQUIRE_SAMPLE,
            self._handler_autosample_acquire_sample)

        # this state would be entered whenever an ACQUIRE_SAMPLE event
        # occurred while in the COMMAND state
        # and will last anywhere from a few seconds to 3
        # minutes depending on instrument and sample type.
        self._protocol_fsm.add_handler(
            SamiProtocolState.POLLED_SAMPLE, SamiProtocolEvent.ENTER,
            self._handler_polled_sample_enter)
        self._protocol_fsm.add_handler(
            SamiProtocolState.POLLED_SAMPLE, SamiProtocolEvent.EXIT,
            self._handler_polled_sample_exit)
        self._protocol_fsm.add_handler(
            SamiProtocolState.POLLED_SAMPLE, SamiProtocolEvent.TAKE_SAMPLE,
            self._handler_take_sample)
        self._protocol_fsm.add_handler(
            SamiProtocolState.POLLED_SAMPLE, SamiProtocolEvent.SUCCESS,
            self._handler_polled_sample_success)
        self._protocol_fsm.add_handler(
            SamiProtocolState.POLLED_SAMPLE, SamiProtocolEvent.TIMEOUT,
            self._handler_polled_sample_timeout)
        ## Events to queue - intended for schedulable events occurring when a sample is being taken
        self._protocol_fsm.add_handler(
            SamiProtocolState.POLLED_SAMPLE, SamiProtocolEvent.ACQUIRE_STATUS,
            self._handler_queue_acquire_status)
        self._protocol_fsm.add_handler(
            SamiProtocolState.POLLED_SAMPLE, SamiProtocolEvent.ACQUIRE_SAMPLE,
            self._handler_queue_acquire_sample)

        # this state would be entered whenever an ACQUIRE_SAMPLE event
        # occurred while in the AUTOSAMPLE state and will last anywhere
        # from 10 seconds to 3 minutes depending on instrument and the
        # type of sampling.
        self._protocol_fsm.add_handler(
            SamiProtocolState.SCHEDULED_SAMPLE, SamiProtocolEvent.ENTER,
            self._handler_scheduled_sample_enter)
        self._protocol_fsm.add_handler(
            SamiProtocolState.SCHEDULED_SAMPLE, SamiProtocolEvent.EXIT,
            self._handler_scheduled_sample_exit)
        self._protocol_fsm.add_handler(
            SamiProtocolState.SCHEDULED_SAMPLE, SamiProtocolEvent.TAKE_SAMPLE,
            self._handler_take_sample)
        self._protocol_fsm.add_handler(
            SamiProtocolState.SCHEDULED_SAMPLE, SamiProtocolEvent.SUCCESS,
            self._handler_scheduled_sample_success)
        self._protocol_fsm.add_handler(
            SamiProtocolState.SCHEDULED_SAMPLE, SamiProtocolEvent.TIMEOUT,
            self._handler_scheduled_sample_timeout)
        ## Events to queue - intended for schedulable events occurring when a sample is being taken
        self._protocol_fsm.add_handler(
            SamiProtocolState.SCHEDULED_SAMPLE, SamiProtocolEvent.ACQUIRE_STATUS,
            self._handler_queue_acquire_status)
        self._protocol_fsm.add_handler(
            SamiProtocolState.SCHEDULED_SAMPLE, SamiProtocolEvent.ACQUIRE_SAMPLE,
            self._handler_queue_acquire_sample)

        # this state would be entered whenever a PUMP_DEIONIZED_WATER event
        # occurred while in the COMMAND state
        self._protocol_fsm.add_handler(
            SamiProtocolState.DEIONIZED_WATER_FLUSH, SamiProtocolEvent.ENTER,
            self._handler_deionized_water_flush_enter)
        self._protocol_fsm.add_handler(
            SamiProtocolState.DEIONIZED_WATER_FLUSH, SamiProtocolEvent.EXIT,
            self._handler_deionized_water_flush_exit)
        self._protocol_fsm.add_handler(
            SamiProtocolState.DEIONIZED_WATER_FLUSH, SamiProtocolEvent.EXECUTE_FLUSH,
            self._handler_deionized_water_flush_execute)
        self._protocol_fsm.add_handler(
            SamiProtocolState.DEIONIZED_WATER_FLUSH, SamiProtocolEvent.SUCCESS,
            self._handler_deionized_water_flush_success)
        self._protocol_fsm.add_handler(
            SamiProtocolState.DEIONIZED_WATER_FLUSH, SamiProtocolEvent.TIMEOUT,
            self._handler_deionized_water_flush_timeout)
        ## Events to queue - intended for schedulable events occurring when a sample is being taken
        self._protocol_fsm.add_handler(
            SamiProtocolState.DEIONIZED_WATER_FLUSH, SamiProtocolEvent.ACQUIRE_STATUS,
            self._handler_queue_acquire_status)

        # this state would be entered whenever a PUMP_REAGENT event
        # occurred while in the COMMAND state
        self._protocol_fsm.add_handler(
            SamiProtocolState.REAGENT_FLUSH, SamiProtocolEvent.ENTER,
            self._handler_reagent_flush_enter)
        self._protocol_fsm.add_handler(
            SamiProtocolState.REAGENT_FLUSH, SamiProtocolEvent.EXIT,
            self._handler_reagent_flush_exit)
        self._protocol_fsm.add_handler(
            SamiProtocolState.REAGENT_FLUSH, SamiProtocolEvent.EXECUTE_FLUSH,
            self._handler_reagent_flush_execute)
        self._protocol_fsm.add_handler(
            SamiProtocolState.REAGENT_FLUSH, SamiProtocolEvent.SUCCESS,
            self._handler_reagent_flush_success)
        self._protocol_fsm.add_handler(
            SamiProtocolState.REAGENT_FLUSH, SamiProtocolEvent.TIMEOUT,
            self._handler_reagent_flush_timeout)
        ## Events to queue - intended for schedulable events occurring when a sample is being taken
        self._protocol_fsm.add_handler(
            SamiProtocolState.REAGENT_FLUSH, SamiProtocolEvent.ACQUIRE_STATUS,
            self._handler_queue_acquire_status)

        # this state would be entered whenever a PUMP_DEIONIZED_WATER event
        # occurred while in the COMMAND state
        self._protocol_fsm.add_handler(
            SamiProtocolState.DEIONIZED_WATER_FLUSH_100ML, SamiProtocolEvent.ENTER,
            self._handler_deionized_water_flush_enter_100ml)
        self._protocol_fsm.add_handler(
            SamiProtocolState.DEIONIZED_WATER_FLUSH_100ML, SamiProtocolEvent.EXIT,
            self._handler_deionized_water_flush_exit_100ml)
        self._protocol_fsm.add_handler(
            SamiProtocolState.DEIONIZED_WATER_FLUSH_100ML, SamiProtocolEvent.EXECUTE_FLUSH,
            self._handler_deionized_water_flush_execute_100ml)
        self._protocol_fsm.add_handler(
            SamiProtocolState.DEIONIZED_WATER_FLUSH_100ML, SamiProtocolEvent.SUCCESS,
            self._handler_deionized_water_flush_success_100ml)
        self._protocol_fsm.add_handler(
            SamiProtocolState.DEIONIZED_WATER_FLUSH_100ML, SamiProtocolEvent.TIMEOUT,
            self._handler_deionized_water_flush_timeout_100ml)
        ## Events to queue - intended for schedulable events occurring when a sample is being taken
        self._protocol_fsm.add_handler(
            SamiProtocolState.DEIONIZED_WATER_FLUSH_100ML, SamiProtocolEvent.ACQUIRE_STATUS,
            self._handler_queue_acquire_status)

        # this state would be entered whenever a PUMP_REAGENT event
        # occurred while in the COMMAND state
        self._protocol_fsm.add_handler(
            SamiProtocolState.REAGENT_FLUSH_100ML, SamiProtocolEvent.ENTER,
            self._handler_reagent_flush_enter_100ml)
        self._protocol_fsm.add_handler(
            SamiProtocolState.REAGENT_FLUSH_100ML, SamiProtocolEvent.EXIT,
            self._handler_reagent_flush_exit_100ml)
        self._protocol_fsm.add_handler(
            SamiProtocolState.REAGENT_FLUSH_100ML, SamiProtocolEvent.EXECUTE_FLUSH,
            self._handler_reagent_flush_execute_100ml)
        self._protocol_fsm.add_handler(
            SamiProtocolState.REAGENT_FLUSH_100ML, SamiProtocolEvent.SUCCESS,
            self._handler_reagent_flush_success_100ml)
        self._protocol_fsm.add_handler(
            SamiProtocolState.REAGENT_FLUSH_100ML, SamiProtocolEvent.TIMEOUT,
            self._handler_reagent_flush_timeout_100ml)
        ## Events to queue - intended for schedulable events occurring when a sample is being taken
        self._protocol_fsm.add_handler(
            SamiProtocolState.REAGENT_FLUSH_100ML, SamiProtocolEvent.ACQUIRE_STATUS,
            self._handler_queue_acquire_status)

        # Construct the parameter dictionary containing device
        # parameters, current parameter values, and set formatting
        # functions.
        self._build_param_dict()
        self._build_command_dict()
        self._build_driver_dict()

        # engineering parameters can be added in sub classes
        self._engineering_parameters = [SamiParameter.AUTO_SAMPLE_INTERVAL]
        self._engineering_parameters.append(SamiParameter.FLUSH_DURATION)
        self._engineering_parameters.append(SamiParameter.PUMP_100ML_CYCLES)

        # Add build handlers for device commands.
        self._add_build_handler(SamiInstrumentCommand.GET_STATUS, self._build_simple_command)
        self._add_build_handler(SamiInstrumentCommand.START_STATUS, self._build_simple_command)  # Never want to do this
        self._add_build_handler(SamiInstrumentCommand.STOP_STATUS, self._build_simple_command)

        self._add_build_handler(SamiInstrumentCommand.GET_CONFIG, self._build_simple_command)
        self._add_build_handler(SamiInstrumentCommand.SET_CONFIG, self._build_simple_command)

        self._add_build_handler(SamiInstrumentCommand.GET_BATTERY_VOLTAGE, self._build_simple_command)
        self._add_build_handler(SamiInstrumentCommand.GET_THERMISTOR_VOLTAGE, self._build_simple_command)

        self._add_build_handler(SamiInstrumentCommand.ERASE_ALL, self._build_simple_command)
        self._add_build_handler(SamiInstrumentCommand.START, self._build_simple_command)
        self._add_build_handler(SamiInstrumentCommand.STOP, self._build_simple_command)
        self._add_build_handler(SamiInstrumentCommand.ACQUIRE_SAMPLE_SAMI, self._build_simple_command)
        self._add_build_handler(SamiInstrumentCommand.ESCAPE_BOOT, self._build_simple_command)
        self._add_build_handler(SamiInstrumentCommand.PUMP_DEIONIZED_WATER_SAMI, self._build_pump_command)
        self._add_build_handler(SamiInstrumentCommand.PUMP_REAGENT_SAMI, self._build_pump_command)
        self._add_build_handler(SamiInstrumentCommand.PUMP_OFF, self._build_simple_command)

        # Add response handlers for device commands.
        self._add_response_handler(SamiInstrumentCommand.GET_STATUS, self._parse_response_get_status)
        self._add_response_handler(SamiInstrumentCommand.STOP_STATUS, self._parse_response_stop_status)
        self._add_response_handler(SamiInstrumentCommand.GET_CONFIG, self._parse_response_get_config)
        self._add_response_handler(SamiInstrumentCommand.SET_CONFIG, self._parse_response_set_config)
        self._add_response_handler(SamiInstrumentCommand.GET_BATTERY_VOLTAGE, self._parse_response_get_battery_voltage)
        self._add_response_handler(SamiInstrumentCommand.GET_THERMISTOR_VOLTAGE, self._parse_response_get_thermistor_voltage)
        self._add_response_handler(SamiInstrumentCommand.ERASE_ALL, self._parse_response_erase_all)
        self._add_response_handler(SamiInstrumentCommand.ACQUIRE_SAMPLE_SAMI, self._parse_response_sample_sami)
        self._add_response_handler(SamiInstrumentCommand.PUMP_DEIONIZED_WATER_SAMI, self._parse_response_pump_deionized_water_sami)
        self._add_response_handler(SamiInstrumentCommand.PUMP_REAGENT_SAMI, self._parse_response_pump_reagent_sami)
        self._add_response_handler(SamiInstrumentCommand.PUMP_OFF, self._parse_response_pump_off_sami)

        # Add sample handlers.

        # commands sent sent to device to be filtered in responses for telnet DA
        self._sent_cmds = []

        self._startup = True

        self._queued_commands = QueuedCommands()

        # initialize scheduler
        if not self._scheduler:
            self.initialize_scheduler()

        self._add_scheduler_event(SamiScheduledJob.ACQUIRE_STATUS, SamiProtocolEvent.ACQUIRE_STATUS)

        # continue __init__ in the sub-class in the specific driver

    def _setup_scheduler_config(self):
        """
        Setup autosample scheduler
        """
        auto_sample_interval = self._param_dict.get(SamiParameter.AUTO_SAMPLE_INTERVAL)

        log.debug('SamiProtocol._setup_scheduler_config(): auto_sample_interval = ' + str(auto_sample_interval))

        if self._startup_config.has_key(DriverConfigKey.SCHEDULER):

            self._startup_config[DriverConfigKey.SCHEDULER][SamiScheduledJob.AUTO_SAMPLE] = {
                DriverSchedulerConfigKey.TRIGGER: {
                    DriverSchedulerConfigKey.TRIGGER_TYPE: TriggerType.INTERVAL,
                    DriverSchedulerConfigKey.SECONDS: auto_sample_interval}
            }

        else:

            self._startup_config[DriverConfigKey.SCHEDULER] = {
                SamiScheduledJob.AUTO_SAMPLE: {
                    DriverSchedulerConfigKey.TRIGGER: {
                        DriverSchedulerConfigKey.TRIGGER_TYPE: TriggerType.INTERVAL,
                        DriverSchedulerConfigKey.SECONDS: auto_sample_interval
                    }
                }
            }

    def _filter_capabilities(self, events):
        """
        Overridden by device specific subclasses.
        """

        raise NotImplementedException()

    ########################################################################
    # Events to queue handlers.
    ########################################################################
    def _handler_queue_acquire_status(self, *args, **kwargs):
        """
        Buffer acquire status command
        """
        log.debug('SamiProtocol._handler_queue_acquire_status(): queueing SamiProtocolEvent.ACQUIRE_STATUS in state ' +
                  self.get_current_state())

        self._queued_commands.status = SamiProtocolEvent.ACQUIRE_STATUS

        next_state = None
        next_agent_state = None
        result = None

        return (next_state, (next_agent_state, result))

    def _handler_queue_acquire_sample(self, *args, **kwargs):
        """
        Buffer acquire sample command
        """
        log.debug('SamiProtocol._handler_queue_acquire_sample(): queueing SamiProtocolEvent.ACQUIRE_SAMPLE in state ' +
                  self.get_current_state())

        self._queued_commands.sample = SamiProtocolEvent.ACQUIRE_SAMPLE

        next_state = None
        next_agent_state = None
        result = None

        return (next_state, (next_agent_state, result))

    ########################################################################
    # Acquire status handler.
    ########################################################################
    def _handler_acquire_status(self, *args, **kwargs):
        """
        Acquire the instrument's status
        """

        log.debug('SamiProtocol._handler_acquire_status()')

        next_state = None
        next_agent_state = None
        result = None

        try:
            self._do_cmd_resp(SamiInstrumentCommand.GET_STATUS, timeout=TIMEOUT, response_regex=REGULAR_STATUS_REGEX_MATCHER)
            self._do_cmd_resp(SamiInstrumentCommand.GET_BATTERY_VOLTAGE, timeout=TIMEOUT, response_regex=BATTERY_VOLTAGE_REGEX_MATCHER)
            self._do_cmd_resp(SamiInstrumentCommand.GET_THERMISTOR_VOLTAGE, timeout=TIMEOUT, response_regex=THERMISTOR_VOLTAGE_REGEX_MATCHER)

            configuration_string_regex = self._get_configuration_string_regex_matcher()
            self._do_cmd_resp(SamiInstrumentCommand.GET_CONFIG, timeout=TIMEOUT, response_regex=configuration_string_regex)

        except InstrumentTimeoutException:

            log.error('SamiProtocol._handler_command_acquire_status(): InstrumentTimeoutException')

        return (next_state, (next_agent_state, result))

    ########################################################################
    # Unknown handlers.
    ########################################################################

    def _handler_unknown_enter(self, *args, **kwargs):
        """
        Enter unknown state.
        """

        log.debug('SamiProtocol._handler_unknown_enter()')

        # Tell driver superclass to send a state change event.
        # Superclass will query the state.
        self._driver_event(DriverAsyncEvent.STATE_CHANGE)

    def _handler_unknown_exit(self, *args, **kwargs):
        """
        Exit unknown state.
        """

        log.debug('SamiProtocol._handler_unknown_exit()')

    def _handler_unknown_discover(self, *args, **kwargs):
        """
        Discover current state; can be UNKNOWN, COMMAND or REGULAR_SAMPLE
        @retval (next_state, result)
        """

        log.debug('SamiProtocol._handler_unknown_discover()')

        next_state = None
        result = None

        log.debug("_handler_unknown_discover: starting discover")
        (next_state, next_agent_state) = self._discover()
        log.debug("_handler_unknown_discover: next agent state: %s", next_agent_state)

        return (next_state, next_agent_state)

    ########################################################################
    # Waiting handlers.
    ########################################################################

    def _handler_waiting_enter(self, *args, **kwargs):
        """
        Enter discover state.
        """
        # Tell driver superclass to send a state change event.
        # Superclass will query the state.

        log.debug('SamiProtocol._handler_waiting_enter()')

        self._driver_event(DriverAsyncEvent.STATE_CHANGE)

        # Test to determine what state we truly are in, command or unknown.
        self._protocol_fsm.on_event(SamiProtocolEvent.DISCOVER)

    def _handler_waiting_exit(self, *args, **kwargs):
        """
        Exit discover state.
        """

        log.debug('SamiProtocol._handler_waiting_exit()')

    def _handler_waiting_discover(self, *args, **kwargs):
        """
        Discover current state; can be UNKNOWN or COMMAND
        @retval (next_state, result)
        """

        log.debug('SamiProtocol._handler_waiting_discover()')

        # Exit states can be either COMMAND or back to UNKNOWN.
        next_state = None
        next_agent_state = None
        result = None

        # try to discover our state
        # currently will retry discovery 6 times every 20 seconds in case SAMI is sampling.
        count = 1
        while count <= 6:
            log.debug("_handler_waiting_discover: starting discover")
            (next_state, next_agent_state) = self._discover()
            if next_state is SamiProtocolState.COMMAND:
                log.debug("_handler_waiting_discover: discover succeeded")
                log.debug("_handler_waiting_discover: next agent state: %s", next_agent_state)
                return (next_state, (next_agent_state, result))
            else:
                log.debug("_handler_waiting_discover: discover failed, attempt %d of 6", count)
                count += 1
                time.sleep(20)

        log.debug("_handler_waiting_discover: discover failed")
        log.debug("_handler_waiting_discover: next agent state: %s", ResourceAgentState.ACTIVE_UNKNOWN)
        return (SamiProtocolState.UNKNOWN, (ResourceAgentState.ACTIVE_UNKNOWN, result))

    ########################################################################
    # Command handlers.
    ########################################################################

    def _handler_command_enter(self, *args, **kwargs):
        """
        Enter command state.
        """

        log.debug('SamiProtocol._handler_command_enter()')

        # Tell driver superclass to send a state change event.
        # Superclass will query the state.
        self._driver_event(DriverAsyncEvent.STATE_CHANGE)

        ## Execute acquire status first if queued
        if self._queued_commands.status is not None:
            command = self._queued_commands.status
            self._queued_commands.status = None
            log.debug('SamiProtocol._handler_command_enter: Raising queued command event: ' + command)
            self._protocol_fsm.on_event(command)

        if self._queued_commands.sample is not None:
            command = self._queued_commands.sample
            self._queued_commands.sample = None
            log.debug('SamiProtocol._handler_command_enter: Raising queued command event: ' + command)
            self._async_agent_state_change(ResourceAgentState.BUSY)
            self._async_raise_fsm_event(command)

    def _handler_command_init_params(self, *args, **kwargs):
        """
        initialize parameters
        """

        log.debug('SamiProtocol._handler_command_init_params()')

        next_state = None
        result = None

        self._init_params()
        return (next_state, result)

    def _handler_command_exit(self, *args, **kwargs):
        """
        Exit command state.
        """

        log.debug('SamiProtocol._handler_command_exit()')

    def _handler_command_get(self, *args, **kwargs):
        """
        Get parameter
        """
        return self._handler_get(*args, **kwargs)

    def _handler_command_set(self, *args, **kwargs):
        """
        Perform a set command.
        @param args[0] parameter : value dict.
        @param args[1] parameter : startup parameters?
        @retval (next_state, result) tuple, (None, None).
        @throws InstrumentParameterException if missing set parameters, if set parameters not ALL and
        not a dict, or if paramter can't be properly formatted.
        @throws InstrumentTimeoutException if device cannot be woken for set command.
        @throws InstrumentProtocolException if set command could not be built or misunderstood.
        """

        next_state = None
        result = None
        startup = False

        try:
            params = args[0]
        except IndexError:
            raise InstrumentParameterException('_handler_command_set Set command requires a parameter dict.')

        log.debug('SamiProtocol._handler_command_set(): params = ' + str(params))

        try:
            startup = args[1]
        except IndexError:
            pass

        self._verify_not_readonly(*args, **kwargs)

        if not isinstance(params, dict):
            raise InstrumentParameterException('Set parameters not a dict.')

        # For each key, val in the dict, issue set command to device.
        # Raise if the command not understood.

        self._set_params(params, startup)

        return next_state, result

    def _handler_command_start_direct(self):
        """
        Start direct access
        """

        log.debug('SamiProtocol._handler_command_start_direct()')

        next_state = SamiProtocolState.DIRECT_ACCESS
        next_agent_state = ResourceAgentState.DIRECT_ACCESS
        result = None
        log.debug("_handler_command_start_direct: entering DA mode")
        return (next_state, (next_agent_state, result))

    def _handler_command_acquire_sample(self):
        """
        Acquire a sample
        """

        log.debug('SamiProtocol._handler_command_acquire_sample()')

        next_state = SamiProtocolState.POLLED_SAMPLE
        next_agent_state = ResourceAgentState.BUSY
        result = None

        return (next_state, (next_agent_state, result))

    def _handler_command_start_autosample(self):
        """
        Start autosample mode (spoofed via use of scheduler)
        """

        log.debug('SamiProtocol._handler_command_start_autosample()')

        ## Note: start ordering seems important for the scheduler, could be a bug in the base code, scheduler blocks
        ##       until last job added to scheduler hits.  Means an autosample could be missed while waiting for
        ##       a sample timeout.  This should be OK.

        # add scheduled tasks
        self._setup_scheduler_config()

        # initialize scheduler
        if not self._scheduler:
            self.initialize_scheduler()

        ## Cannot do in exit because we could be transitioning to the scheduled sample state
        self._add_scheduler_event(SamiScheduledJob.AUTO_SAMPLE, SamiProtocolEvent.ACQUIRE_SAMPLE)

        ## Make sure a sample is taken as soon as autosample mode is entered.
        self._queued_commands.sample = SamiProtocolEvent.ACQUIRE_SAMPLE

        next_state = SamiProtocolState.AUTOSAMPLE
        next_agent_state = ResourceAgentState.STREAMING
        result = None
        log.debug("_handler_command_start_autosample: entering Autosample mode")

        return (next_state, (next_agent_state, result))

    def _handler_command_deionized_water_flush(self):
        """
        Flush with deionized water
        """

        log.debug('SamiProtocol._handler_command_deionized_water_flush()')

        next_state = SamiProtocolState.DEIONIZED_WATER_FLUSH
        next_agent_state = ResourceAgentState.BUSY
        result = None

        return (next_state, (next_agent_state, result))

    def _handler_command_reagent_flush(self):
        """
        Flush with reagent
        """

        log.debug('SamiProtocol._handler_command_reagent_flush()')

        next_state = SamiProtocolState.REAGENT_FLUSH
        next_agent_state = ResourceAgentState.BUSY
        result = None

        return (next_state, (next_agent_state, result))

    def _handler_command_deionized_water_flush_100ml(self):
        """
        Flush with deionized water
        """

        log.debug('SamiProtocol._handler_command_deionized_water_flush_100ml()')

        next_state = SamiProtocolState.DEIONIZED_WATER_FLUSH_100ML
        next_agent_state = ResourceAgentState.BUSY
        result = None

        return (next_state, (next_agent_state, result))

    def _handler_command_reagent_flush_100ml(self):
        """
        Flush with reagent
        """

        log.debug('SamiProtocol._handler_command_reagent_flush_100ml()')

        next_state = SamiProtocolState.REAGENT_FLUSH_100ML
        next_agent_state = ResourceAgentState.BUSY
        result = None

        return (next_state, (next_agent_state, result))

    ########################################################################
    # Direct access handlers.
    ########################################################################

    def _handler_direct_access_enter(self, *args, **kwargs):
        """
        Enter direct access state.
        """

        log.debug('SamiProtocol._handler_direct_access_enter')

        # Tell driver superclass to send a state change event.
        # Superclass will query the state.
        self._driver_event(DriverAsyncEvent.STATE_CHANGE)

        self._sent_cmds = []

    def _handler_direct_access_exit(self, *args, **kwargs):
        """
        Exit direct access state.
        """

        log.debug('SamiProtocol._handler_direct_access_exit')

    def _handler_direct_access_execute_direct(self, data):
        """
        """

        log.debug('SamiProtocol._handler_direct_access_execute_direct')

        next_state = None
        result = None
        next_agent_state = None

        self._do_cmd_direct(data)

        # add sent command to list for 'echo' filtering in callback
        self._sent_cmds.append(data)

        return (next_state, (next_agent_state, result))

    def _handler_direct_access_stop_direct(self):
        """
        @throw InstrumentProtocolException on invalid command
        """

        log.debug('SamiProtocol._handler_direct_access_stop_direct')

        next_state = None
        result = None

        log.debug("_handler_direct_access_stop_direct: starting discover")
        (next_state, next_agent_state) = self._discover()
        log.debug("_handler_direct_access_stop_direct: next agent state: %s", next_agent_state)

        return (next_state, (next_agent_state, result))

    ########################################################################
    # Autosample handlers.
    ########################################################################


    def _handler_autosample_enter(self):
        """
        Enter Autosample state
        """

        log.debug('SamiProtocol._handler_autosample_enter')

        # Tell driver superclass to send a state change event.
        # Superclass will query the state.
        self._driver_event(DriverAsyncEvent.STATE_CHANGE)

        ## Capture a sample upon entering autosample mode.  An ACQUIRE_SAMPLE event should have been queued in the start
        ## autosample command handler.

        ## Execute acquire status first if queued
        if self._queued_commands.status is not None:
            command = self._queued_commands.status
            self._queued_commands.status = None
            log.debug('SamiProtocol._handler_autosample_enter: Raising queued command event: ' + command)
            self._protocol_fsm.on_event(command)

        if self._queued_commands.sample is not None:
            command = self._queued_commands.sample
            self._queued_commands.sample = None
            log.debug('SamiProtocol._handler_autosample_enter: Raising queued command event: ' + command)
            self._async_agent_state_change(ResourceAgentState.BUSY)
            self._async_raise_fsm_event(command)

    def _handler_autosample_exit(self, *args, **kwargs):
        """
        Exit autosample state
        """

        log.debug('SamiProtocol._handler_autosample_exit')

    def _handler_autosample_stop(self, *args, **kwargs):
        """
        Stop autosample
        """

        log.debug('SamiProtocol._handler_autosample_stop')
        log.debug('SamiProtocol._handler_autosample_stop: Move to command state')

        ## Cannot do in exit because we could be transitioning to the scheduled sample state
        self._remove_scheduler(SamiScheduledJob.AUTO_SAMPLE)

        next_state = SamiProtocolState.COMMAND
        next_agent_state = ResourceAgentState.COMMAND
        result = None

        return (next_state, (next_agent_state, result))

    def _handler_autosample_acquire_sample(self, *args, **kwargs):
        """
        While in autosample mode, poll for samples using the scheduler
        """

        log.debug('SamiProtocol._handler_autosample_acquire_sample')

        next_state = SamiProtocolState.SCHEDULED_SAMPLE
        next_agent_state = ResourceAgentState.BUSY
        result = None

        return (next_state, (next_agent_state, result))

    ########################################################################
    # Generic Take Sample handler used in polled and autosample states
    ########################################################################

    def _handler_take_sample(self, *args, **kwargs):
        """
        Acquire sample
        """

        log.debug('SamiProtocol._handler_take_sample() ENTER')
        log.debug('SamiProtocol._handler_take_sample(): CURRENT_STATE == ' + self.get_current_state())

        try:
            self._take_regular_sample()
            log.debug('SamiProtocol._handler_take_sample(): SUCCESS')
            self._async_raise_fsm_event(SamiProtocolEvent.SUCCESS)
        except InstrumentTimeoutException:
            log.error('SamiProtocol._handler_take_sample(): TIMEOUT')
            self._async_raise_fsm_event(SamiProtocolEvent.TIMEOUT)

        log.debug('SamiProtocol._handler_take_sample() EXIT')

        return None, None

    ########################################################################
    # Polled Sample handlers.
    ########################################################################

    def _handler_polled_sample_enter(self, *args, **kwargs):
        """
        Enter state.
        """

        log.debug('SamiProtocol._handler_polled_sample_enter')

        self._async_raise_fsm_event(SamiProtocolEvent.TAKE_SAMPLE)

        # Tell driver superclass to send a state change event.
        # Superclass will query the state.
        self._driver_event(DriverAsyncEvent.STATE_CHANGE)

    def _handler_polled_sample_exit(self, *args, **kwargs):
        """
        Exit state.
        """

        log.debug('SamiProtocol._handler_polled_sample_exit')

    def _handler_polled_sample_success(self, *args, **kwargs):
        """
        Successfully received a sample from SAMI
        """

        log.debug('SamiProtocol._handler_polled_sample_success')

        next_state = SamiProtocolState.COMMAND
        next_agent_state = ResourceAgentState.COMMAND

        self._async_agent_state_change(next_agent_state)

        return (next_state, next_agent_state)

    def _handler_polled_sample_timeout(self, *args, **kwargs):
        """
        Sample timeout occurred.
        """

        log.error('SamiProtocol._handler_polled_sample_timeout(): Sample timeout occurred')

        next_state = SamiProtocolState.COMMAND
        next_agent_state = ResourceAgentState.COMMAND

        self._async_agent_state_change(next_agent_state)

        return (next_state, next_agent_state)

    ########################################################################
    # Scheduled Sample handlers.
    ########################################################################

    def _handler_scheduled_sample_enter(self, *args, **kwargs):
        """
        Enter state.
        """

        log.debug('SamiProtocol._handler_scheduled_sample_enter')

        self._async_raise_fsm_event(SamiProtocolEvent.TAKE_SAMPLE)

        # Tell driver superclass to send a state change event.
        # Superclass will query the state.
        self._driver_event(DriverAsyncEvent.STATE_CHANGE)

    def _handler_scheduled_sample_exit(self, *args, **kwargs):
        """
        Exit state.
        """

        log.debug('SamiProtocol._handler_scheduled_sample_exit')

    def _handler_scheduled_sample_success(self, *args, **kwargs):
        """
        Successfully recieved a sample from SAMI
        """

        log.debug('SamiProtocol._handler_scheduled_sample_success')

        next_state = SamiProtocolState.AUTOSAMPLE
        next_agent_state = ResourceAgentState.STREAMING

        self._async_agent_state_change(next_agent_state)

        return (next_state, next_agent_state)

    def _handler_scheduled_sample_timeout(self, *args, **kwargs):
        """
        Sample timeout occurred.
        """

        log.error('SamiProtocol._handler_scheduled_sample_timeout(): Sample timeout occurred')

        next_state = SamiProtocolState.AUTOSAMPLE
        next_agent_state = ResourceAgentState.STREAMING

        self._async_agent_state_change(next_agent_state)

        return (next_state, next_agent_state)

    ########################################################################
    # Deionized water flush handlers.
    ########################################################################

    def _handler_deionized_water_flush_enter(self, *args, **kwargs):
        """
        Enter state.
        """

        log.debug('SamiProtocol._handler_deionized_water_flush_enter')

        self._async_raise_fsm_event(SamiProtocolEvent.EXECUTE_FLUSH)

        # Tell driver superclass to send a state change event.
        # Superclass will query the state.
        self._driver_event(DriverAsyncEvent.STATE_CHANGE)

    def _handler_deionized_water_flush_exit(self, *args, **kwargs):
        """
        Exit state.
        """

        log.debug('SamiProtocol._handler_deionized_water_flush_exit')

    def _handler_deionized_water_flush_success(self, *args, **kwargs):
        """
        Successfully received a sample from SAMI
        """

        log.debug('SamiProtocol._handler_deionized_water_flush_success')

        next_state = SamiProtocolState.COMMAND
        next_agent_state = ResourceAgentState.COMMAND

        self._async_agent_state_change(next_agent_state)

        return (next_state, next_agent_state)

    def _handler_deionized_water_flush_timeout(self, *args, **kwargs):
        """
        Sample timeout occurred.
        """

        log.error('SamiProtocol._handler_deionized_water_flush_timeout(): Deionized water flush timeout occurred')

        next_state = SamiProtocolState.COMMAND
        next_agent_state = ResourceAgentState.COMMAND

        self._async_agent_state_change(next_agent_state)

        return (next_state, next_agent_state)

    def _handler_deionized_water_flush_execute(self, *args, **kwargs):
        """
        Execute pump command, sleep to make sure it completes and make sure pump is off
        """

        try:

            param = SamiParameter.FLUSH_DURATION
            flush_duration = self._param_dict.get(param)
            flush_duration_str = self._param_dict.format(param, flush_duration)
            flush_duration_seconds = flush_duration * PUMP_DURATION_UNITS

            log.debug('SamiProtocol._handler_deionized_water_flush_execute(): flush duration param = %s, seconds = %s' % (flush_duration, flush_duration_seconds))

            # Add 5 seconds to timeout make sure pump completes.
            flush_timeout = flush_duration_seconds + PUMP_TIMEOUT_OFFSET

            start_time = time.time()
            self._do_cmd_resp(SamiInstrumentCommand.PUMP_DEIONIZED_WATER_SAMI, flush_duration_str, timeout=flush_timeout, response_regex=NEW_LINE_REGEX_MATCHER)
            pump_time = time.time() - start_time
            log.debug('SamiProtocol._handler_deionized_water_flush_execute(): pump time = %s' % pump_time)

            # Make sure pump is off
            self._do_cmd_resp(SamiInstrumentCommand.PUMP_OFF, timeout=TIMEOUT, response_regex=NEW_LINE_REGEX_MATCHER)

            log.debug('SamiProtocol._handler_deionized_water_flush_execute(): SUCCESS')
            self._async_raise_fsm_event(SamiProtocolEvent.SUCCESS)
        except InstrumentTimeoutException:
            log.error('SamiProtocol._handler_deionized_water_flush_execute(): TIMEOUT')
            self._async_raise_fsm_event(SamiProtocolEvent.TIMEOUT)

        return None, None

    ########################################################################
    # Reagent flush handlers.
    ########################################################################

    def _handler_reagent_flush_enter(self, *args, **kwargs):
        """
        Enter state.
        """

        log.debug('SamiProtocol._handler_reagent_flush_enter')

        self._async_raise_fsm_event(SamiProtocolEvent.EXECUTE_FLUSH)

        # Tell driver superclass to send a state change event.
        # Superclass will query the state.
        self._driver_event(DriverAsyncEvent.STATE_CHANGE)

    def _handler_reagent_flush_exit(self, *args, **kwargs):
        """
        Exit state.
        """

        log.debug('SamiProtocol._handler_reagent_flush_exit')

    def _handler_reagent_flush_success(self, *args, **kwargs):
        """
        Successfully received a sample from SAMI
        """

        log.debug('SamiProtocol._handler_reagent_flush_success')

        next_state = SamiProtocolState.COMMAND
        next_agent_state = ResourceAgentState.COMMAND

        self._async_agent_state_change(next_agent_state)

        return (next_state, next_agent_state)

    def _handler_reagent_flush_timeout(self, *args, **kwargs):
        """
        Sample timeout occurred.
        """

        log.error('SamiProtocol._handler_reagent_flush_timeout(): Reagent flush timeout occurred')

        next_state = SamiProtocolState.COMMAND
        next_agent_state = ResourceAgentState.COMMAND

        self._async_agent_state_change(next_agent_state)

        return (next_state, next_agent_state)

    def _handler_reagent_flush_execute(self, *args, **kwargs):
        """
        Execute pump command, sleep to make sure it completes and make sure pump is off
        """

        try:
            param = SamiParameter.FLUSH_DURATION
            flush_duration = self._param_dict.get(param)
            flush_duration_str = self._param_dict.format(param, flush_duration)
            flush_duration_seconds = flush_duration * PUMP_DURATION_UNITS

            log.debug('SamiProtocol._handler_reagent_flush_execute(): flush duration param = %s, seconds = %s' % (flush_duration, flush_duration_seconds))

            # Add 5 seconds to timeout to make sure pump completes.
            flush_timeout = flush_duration_seconds + PUMP_TIMEOUT_OFFSET

            start_time = time.time()
            self._do_cmd_resp(SamiInstrumentCommand.PUMP_REAGENT_SAMI, flush_duration_str, timeout=flush_timeout, response_regex=NEW_LINE_REGEX_MATCHER)
            pump_time = time.time() - start_time
            log.debug('SamiProtocol._handler_reagent_flush_execute(): pump time = %s' % pump_time)

            # Make sure pump is off
            self._do_cmd_resp(SamiInstrumentCommand.PUMP_OFF, timeout=TIMEOUT, response_regex=NEW_LINE_REGEX_MATCHER)

            log.debug('SamiProtocol._handler_reagent_flush_execute(): SUCCESS')
            self._async_raise_fsm_event(SamiProtocolEvent.SUCCESS)
        except InstrumentTimeoutException:
            log.error('SamiProtocol._handler_reagent_flush_execute(): TIMEOUT')
            self._async_raise_fsm_event(SamiProtocolEvent.TIMEOUT)

        return None, None

    ########################################################################
    # Deionized water flush 100 ml handlers.
    ########################################################################

    def _handler_deionized_water_flush_enter_100ml(self, *args, **kwargs):
        """
        Enter state.
        """

        log.debug('SamiProtocol._handler_deionized_water_flush_enter_100ml')

        self._async_raise_fsm_event(SamiProtocolEvent.EXECUTE_FLUSH)

        # Tell driver superclass to send a state change event.
        # Superclass will query the state.
        self._driver_event(DriverAsyncEvent.STATE_CHANGE)

    def _handler_deionized_water_flush_exit_100ml(self, *args, **kwargs):
        """
        Exit state.
        """

        log.debug('SamiProtocol._handler_deionized_water_flush_exit_100ml')

    def _handler_deionized_water_flush_success_100ml(self, *args, **kwargs):
        """
        Successfully received a sample from SAMI
        """

        log.debug('SamiProtocol._handler_deionized_water_flush_success_100ml')

        next_state = SamiProtocolState.COMMAND
        next_agent_state = ResourceAgentState.COMMAND

        self._async_agent_state_change(next_agent_state)

        return (next_state, next_agent_state)

    def _handler_deionized_water_flush_timeout_100ml(self, *args, **kwargs):
        """
        Sample timeout occurred.
        """

        log.error('SamiProtocol._handler_deionized_water_flush_timeout_100ml(): Deionized water flush timeout occurred')

        next_state = SamiProtocolState.COMMAND
        next_agent_state = ResourceAgentState.COMMAND

        self._async_agent_state_change(next_agent_state)

        return (next_state, next_agent_state)

    def _handler_deionized_water_flush_execute_100ml(self, *args, **kwargs):
        """
        Execute pump command, sleep to make sure it completes and make sure pump is off
        """

        try:

            pump_100ml_cycles = self._param_dict.get(SamiParameter.PUMP_100ML_CYCLES)
            log.debug('SamiProtocol._handler_deionized_water_flush_execute_100ml(): pump 100ml cycles = %s' % pump_100ml_cycles)

            flush_duration = PUMP_DURATION_50ML
            flush_duration_str = str(flush_duration)
            flush_duration_seconds = flush_duration * PUMP_DURATION_UNITS
            log.debug('SamiProtocol._handler_deionized_water_flush_execute_100ml(): flush duration param = %s, seconds = %s' % (flush_duration, flush_duration_seconds))

            # Add 5 seconds to timeout make sure pump completes.
            flush_timeout = flush_duration_seconds + PUMP_TIMEOUT_OFFSET

            for pump_num in range(pump_100ml_cycles):
                start_time = time.time()
                self._do_cmd_resp(SamiInstrumentCommand.PUMP_DEIONIZED_WATER_SAMI, flush_duration_str, timeout=flush_timeout, response_regex=NEW_LINE_REGEX_MATCHER)
                pump_time = time.time() - start_time
                log.debug('SamiProtocol._handler_deionized_water_flush_execute_100ml(): pump num = %s, pump time = %s' % (pump_num, pump_time))
                time.sleep(PUMP_SLEEP_50ML)

                start_time = time.time()
                self._do_cmd_resp(SamiInstrumentCommand.PUMP_DEIONIZED_WATER_SAMI, flush_duration_str, timeout=flush_timeout, response_regex=NEW_LINE_REGEX_MATCHER)
                pump_time = time.time() - start_time
                log.debug('SamiProtocol._handler_deionized_water_flush_execute_100ml(): pump num = %s, pump time = %s' % (pump_num, pump_time))
                time.sleep(PUMP_SLEEP_50ML)

            # Make sure pump is off
            self._do_cmd_resp(SamiInstrumentCommand.PUMP_OFF, timeout=TIMEOUT, response_regex=NEW_LINE_REGEX_MATCHER)

            log.debug('SamiProtocol._handler_deionized_water_flush_execute_100ml(): SUCCESS')
            self._async_raise_fsm_event(SamiProtocolEvent.SUCCESS)
        except InstrumentTimeoutException:
            log.error('SamiProtocol._handler_deionized_water_flush_execute_100ml(): TIMEOUT')
            self._async_raise_fsm_event(SamiProtocolEvent.TIMEOUT)

        return None, None

    ########################################################################
    # Reagent flush 100 ml handlers.
    ########################################################################

    def _handler_reagent_flush_enter_100ml(self, *args, **kwargs):
        """
        Enter state.
        """

        log.debug('SamiProtocol._handler_reagent_flush_enter_100ml')

        self._async_raise_fsm_event(SamiProtocolEvent.EXECUTE_FLUSH)

        # Tell driver superclass to send a state change event.
        # Superclass will query the state.
        self._driver_event(DriverAsyncEvent.STATE_CHANGE)

    def _handler_reagent_flush_exit_100ml(self, *args, **kwargs):
        """
        Exit state.
        """

        log.debug('SamiProtocol._handler_reagent_flush_exit_100ml')

    def _handler_reagent_flush_success_100ml(self, *args, **kwargs):
        """
        Successfully received a sample from SAMI
        """

        log.debug('SamiProtocol._handler_reagent_flush_success_100ml')

        next_state = SamiProtocolState.COMMAND
        next_agent_state = ResourceAgentState.COMMAND

        self._async_agent_state_change(next_agent_state)

        return (next_state, next_agent_state)

    def _handler_reagent_flush_timeout_100ml(self, *args, **kwargs):
        """
        Sample timeout occurred.
        """

        log.error('SamiProtocol._handler_reagent_flush_timeout_100ml(): Reagent flush timeout occurred')

        next_state = SamiProtocolState.COMMAND
        next_agent_state = ResourceAgentState.COMMAND

        self._async_agent_state_change(next_agent_state)

        return (next_state, next_agent_state)

    def _handler_reagent_flush_execute_100ml(self, *args, **kwargs):
        """
        Execute pump command, sleep to make sure it completes and make sure pump is off
        """

        try:

            pump_100ml_cycles = self._param_dict.get(SamiParameter.PUMP_100ML_CYCLES)
            log.debug('SamiProtocol._handler_reagent_flush_execute_100ml(): pump 100ml cycles = %s' % pump_100ml_cycles)

            flush_duration = PUMP_DURATION_50ML
            flush_duration_str = str(flush_duration)
            flush_duration_seconds = flush_duration * PUMP_DURATION_UNITS
            log.debug('SamiProtocol._handler_reagent_flush_execute_100ml(): flush duration param = %s, seconds = %s' % (flush_duration, flush_duration_seconds))

            # Add 5 seconds to timeout to make sure pump completes.
            flush_timeout = flush_duration_seconds + PUMP_TIMEOUT_OFFSET

            for pump_num in range(pump_100ml_cycles):
                start_time = time.time()
                self._do_cmd_resp(SamiInstrumentCommand.PUMP_REAGENT_SAMI, flush_duration_str, timeout=flush_timeout, response_regex=NEW_LINE_REGEX_MATCHER)
                pump_time = time.time() - start_time
                log.debug('SamiProtocol._handler_deionized_water_flush_execute_100ml(): pump num = %s, pump time = %s' % (pump_num, pump_time))
                time.sleep(PUMP_SLEEP_50ML)

                start_time = time.time()
                self._do_cmd_resp(SamiInstrumentCommand.PUMP_REAGENT_SAMI, flush_duration_str, timeout=flush_timeout, response_regex=NEW_LINE_REGEX_MATCHER)
                pump_time = time.time() - start_time
                log.debug('SamiProtocol._handler_deionized_water_flush_execute_100ml(): pump num = %s, pump time = %s' % (pump_num, pump_time))
                time.sleep(PUMP_SLEEP_50ML)

            # Make sure pump is off
            self._do_cmd_resp(SamiInstrumentCommand.PUMP_OFF, timeout=TIMEOUT, response_regex=NEW_LINE_REGEX_MATCHER)

            log.debug('SamiProtocol._handler_reagent_flush_execute_100ml(): SUCCESS')
            self._async_raise_fsm_event(SamiProtocolEvent.SUCCESS)
        except InstrumentTimeoutException:
            log.error('SamiProtocol._handler_reagent_flush_execute_100ml(): TIMEOUT')
            self._async_raise_fsm_event(SamiProtocolEvent.TIMEOUT)

        return None, None

    ####################################################################
    # Build Command & Parameter dictionary
    ####################################################################

    def _build_command_dict(self):
        """
        Populate the command dictionary with command.
        """

        log.debug('SamiProtocol._build_command_dict')

        self._cmd_dict.add(SamiCapability.ACQUIRE_SAMPLE, display_name="acquire sample")
        self._cmd_dict.add(SamiCapability.ACQUIRE_STATUS, display_name="acquire status")
        self._cmd_dict.add(SamiCapability.START_AUTOSAMPLE, display_name="start autosample")
        self._cmd_dict.add(SamiCapability.STOP_AUTOSAMPLE, display_name="stop autosample")
        self._cmd_dict.add(SamiCapability.DEIONIZED_WATER_FLUSH, display_name="deionized water flush")
        self._cmd_dict.add(SamiCapability.REAGENT_FLUSH, display_name="reagent flush")
        self._cmd_dict.add(SamiCapability.DEIONIZED_WATER_FLUSH_100ML, display_name="deionized water flush 100 ml")
        self._cmd_dict.add(SamiCapability.REAGENT_FLUSH_100ML, display_name="reagent flush 100 ml")

    def _build_driver_dict(self):
        """
        Populate the driver dictionary with options
        """

        log.debug('SamiProtocol._build_driver_dict')

        self._driver_dict.add(DriverDictKey.VENDOR_SW_COMPATIBLE, True)

    ########################################################################
    # Command handlers.
    ########################################################################

    def _build_pump_command(self, cmd, duration):

        pump_command = cmd + ',' + duration + NEWLINE

        log.debug('SamiProtocol._build_pump_command(): pump command = %s' % pump_command)

        return pump_command

    def _build_simple_command(self, cmd):
        """
        Build handler for basic SAMI commands.
        @param cmd the simple SAMI command to format.
        @retval The command to be sent to the device.
        """

        log.debug('SamiProtocol._build_simple_command')

        return cmd + NEWLINE

    ########################################################################
    # Response handlers.
    ########################################################################

    def _parse_response_get_battery_voltage(self, response, prompt):
        """
        Parse get battery voltage instrument command response
        """
        log.debug('SamiProtocol._parse_response_get_battery_voltage')

        try:
            self._extract_sample(SamiBatteryVoltageDataParticle, BATTERY_VOLTAGE_REGEX_MATCHER, response + NEWLINE, None)
        except Exception as ex:
            log.error('Unexpected exception generating SamiBatteryVoltageDataParticle: ' + str(ex))

        return response

    def _parse_response_get_thermistor_voltage(self, response, prompt):
        """
        Parse get thermistor voltage instrument command response
        """
        log.debug('SamiProtocol._parse_response_get_thermistor_voltage')

        try:
            self._extract_sample(SamiThermistorVoltageDataParticle, THERMISTOR_VOLTAGE_REGEX_MATCHER, response + NEWLINE, None)
        except Exception as ex:
            log.error('Unexpected exception generating SamiThermistorVoltageDataParticle: ' + str(ex))

        return response

    def _parse_response_get_status(self, response, prompt):
        """
        Parse get status instrument command response
        """
        log.debug('SamiProtocol._parse_response_get_status: response = ' + repr(response))
        return response

    def _parse_response_stop_status(self, response, prompt):
        """
        Parse stop status instrument command response
        """
        log.debug('SamiProtocol._parse_response_stop_status: response = ' + repr(response))
        log.debug('SamiProtocol._parse_response_stop_status: prompt   = ' + repr(prompt))
        return response

    def _parse_response_get_config(self, response, prompt):
        """
        Parse get config instrument command response
        """
        log.debug('SamiProtocol._parse_response_get_config')
        return response

    def _parse_response_set_config(self, response, prompt):
        """
        Parse set config instrument command response
        """
        log.debug('SamiProtocol._parse_response_set_config')

    def _parse_response_erase_all(self, response, prompt):
        """
        Parse erase all instrument command response
        """
        log.debug('SamiProtocol._parse_response_erase_all')

    def _parse_response_sample_sami(self, response, prompt):
        """
        Parse take sample instrument command response
        """
        log.debug('SamiProtocol._parse_response_sample_sami')

    def _parse_response_pump_deionized_water_sami(self, response, prompt):
        """
        Parse response to pump deionized water command
        """
        log.debug('SamiProtocol._parse_response_pump_deionized_water_sami')

    def _parse_response_pump_reagent_sami(self, response, prompt):
        """
        Parse response to pump reagent command
        """
        log.debug('SamiProtocol._parse_response_pump_reagent_sami')

    def _parse_response_pump_off_sami(self, response, prompt):
        """
        Parse response to pump off command
        """
        log.debug('SamiProtocol._parse_response_pump_off_sami')

    def _wakeup(self, timeout=0, delay=0):
        """
        Override wakeup instrument processing
        @param timeout not used
        @param delay not used
        """
        # Send 2 newlines to wake up SAMI.
        log.debug('SamiProtocol._wakeup: Send first newline to wake up')
        self._do_cmd_direct(NEWLINE)
        time.sleep(WAKEUP_DELAY)
        log.debug('SamiProtocol._wakeup: Send second newline to wake up')
        self._do_cmd_direct(NEWLINE)
        time.sleep(WAKEUP_DELAY)

    def apply_startup_params(self):

        """
        Apply the startup values previously stored in the protocol to
        the running config of the live instrument.
        @raise InstrumentParameterException If attempt to set init value in any state but command
        """

        log.debug('SamiProtocol.apply_startup_params: CURRENT STATE: %s' % self.get_current_state())
        if self.get_current_state() != SamiProtocolState.COMMAND:
            raise InstrumentProtocolException("Not in command. Unable to apply startup params")

        startup_config = self.get_startup_config()

        log.debug('SamiProtocol.apply_startup_params: startup_config = ' + str(startup_config))

        self._set_params(startup_config)

    def set_init_params(self, config):
        """
        Overridden to throw an exception if there is an attempt to set a read only parameter
        Set the initialization parameters to the given values in the protocol
        parameter dictionary.
        @param config The parameter_name/value to set in the initialization
            fields of the parameter dictionary
        @raise InstrumentParameterException If the config cannot be set
        """
        if not isinstance(config, dict):
            raise InstrumentParameterException("Invalid init config format")

        self._startup_config = config

        param_config = config.get(DriverConfigKey.PARAMETERS)

        if param_config:
            self._verify_not_readonly(param_config, startup=True)
            for name in param_config.keys():
                log.debug("Setting init value for %s to %s", name, param_config[name])
                self._param_dict.set_init_value(name, param_config[name])

    def _discover(self):
        """
        Discover current state; can be UNKNOWN, COMMAND or DISCOVER
        @retval (next_state, result)
        """

        next_state = None
        next_agent_state = None

        ## Clear command queue
        self._queued_commands.reset()

        ## Set default and startup config values in param_dict to establish a baseline
        if self._startup:

            old_config_params = self._param_dict.get_all()
            log.debug('SamiProtocol._discover: old_config_params = ' + str(old_config_params))

            for (key, val) in old_config_params.iteritems():
                self._param_dict.set_value(key, self._param_dict.get_config_value(key))

            new_config_params = self._param_dict.get_all()
            log.debug('SamiProtocol._discover: new_config_params = ' + str(new_config_params))

            self._startup = False

        # Stop status and check for boot prompt
        try:
            response = self._do_cmd_resp(SamiInstrumentCommand.STOP_STATUS, timeout=2, expected_prompt=Prompt.BOOT_PROMPT)

            log.debug('SamiProtocol._discover: boot prompt present = ' + str(response))
            self._do_cmd_direct(SamiInstrumentCommand.ESCAPE_BOOT + NEWLINE)

        except InstrumentTimeoutException:
            log.debug('SamiProtocol._discover: boot prompt did not occur.')

        try:

            log.debug('SamiProtocol._discover')

            log.debug('SamiProtocol._discover: _set_configuration BEGIN')
            self._set_configuration()
            log.debug('SamiProtocol._discover: _set_configuration END')

        except InstrumentTimeoutException:

            log.error('SamiProtocol._discover: InstrumentTimeoutException - retry in WAITING state')

            next_state = SamiProtocolState.WAITING
            next_agent_state = ResourceAgentState.BUSY

        except InstrumentProtocolException:

            log.error('SamiProtocol._discover: InstrumentProtocolException - retry in WAITING state')

            next_state = SamiProtocolState.WAITING
            next_agent_state = ResourceAgentState.BUSY

        else:

            log.debug('SamiProtocol._discover: Move to command state')

            next_state = SamiProtocolState.COMMAND
            next_agent_state = ResourceAgentState.COMMAND

        return (next_state, next_agent_state)

    def _set_params(self, *args, **kwargs):
        """
        Override set_params
        @param params parameters to set
        @raise InstrumentParameterException params not provided as dict
        """
        log.debug('SamiProtocol._set_params')

        try:
            params = args[0]
        except IndexError:
            raise InstrumentParameterException('Set command requires a parameter dict.')

        log.debug('SamiProtocol._set_params(): params = ' + str(params))

        self._check_for_engineering_parameters(params)

        if len(params) > 0:
            self._set_configuration(override_params_dict=params)
        else:
            log.debug('SamiProtocol._set_params(): No parameters to reconfigure instrument.')

    def _set_configuration(self, override_params_dict = {}):
        """
        Set configuration on the instrument.
        @param override_params_dict parameters to override in config string
        """

        ## Build configuration string sequence.
        ## configuration_string = self._build_configuration_string_specific()
        configuration_string = self._build_configuration_string_base(
            self._get_specific_configuration_string_parameters(),
            override_params_dict)

        # Make sure automatic-status updates are off. This will stop the
        # broadcast of information while we are trying to get/set data.
        log.debug('SamiProtocol._set_configuration: STOP_STATUS_PERIODIC')
        self._do_cmd_no_resp(SamiInstrumentCommand.STOP_STATUS)

        # Set response timeout to 10 seconds. Should be immediate if
        # communications are enabled and the instrument is not sampling.
        # Otherwise, sampling can take up to ~ minutes to complete. Partial
        # strings are output during that time period.  SAMI blocks during sampling.
        # No other commands are accepted.
        # Send the configuration string

        # Acquire the current instrument status
        log.debug('SamiProtocol._set_configuration: GET_STATUS')
        status = self._do_cmd_resp(SamiInstrumentCommand.GET_STATUS, timeout=TIMEOUT, response_regex=REGULAR_STATUS_REGEX_MATCHER)
        log.debug('SamiProtocol._set_configuration: status = ' + status)

        log.debug('SamiProtocol._set_configuration: ERASE_ALL')
        #Erase memory before setting configuration
        self._do_cmd_direct(SamiInstrumentCommand.ERASE_ALL + NEWLINE)

        log.debug('SamiProtocol._set_configuration: SET_CONFIG')
        self._do_cmd_resp(SamiInstrumentCommand.SET_CONFIG, timeout=TIMEOUT, response_regex=NEW_LINE_REGEX_MATCHER)
        # Important: Need to do right after to prevent bricking
        self._do_cmd_direct(configuration_string + CONFIG_TERMINATOR + NEWLINE)

        ## Stop auto status again, it is restarted after setting the configuration data
        log.debug('SamiProtocol._set_configuration: STOP_STATUS_PERIODIC again')
        self._do_cmd_no_resp(SamiInstrumentCommand.STOP_STATUS)

        ## Verify configuration and update parameter dictionary if it is set correctly
        self._verify_and_update_configuration(configuration_string)

        # Send status, don't need to send configuration, it's sent in _verify_and_update_configuration()
        self._do_cmd_resp(SamiInstrumentCommand.GET_STATUS, timeout=TIMEOUT, response_regex=REGULAR_STATUS_REGEX_MATCHER)
        self._do_cmd_resp(SamiInstrumentCommand.GET_BATTERY_VOLTAGE, timeout=TIMEOUT, response_regex=BATTERY_VOLTAGE_REGEX_MATCHER)
        self._do_cmd_resp(SamiInstrumentCommand.GET_THERMISTOR_VOLTAGE, timeout=TIMEOUT, response_regex=THERMISTOR_VOLTAGE_REGEX_MATCHER)

    @staticmethod
    def _int_to_hexstring(val, slen):
        """
        Write an integer value to an ASCIIHEX string formatted for SAMI
        configuration set operations.
        @param val the integer value to convert.
        @param slen the required length of the returned string.
        @retval an integer string formatted in ASCIIHEX for SAMI configuration
        set operations.
        @throws InstrumentParameterException if the integer and string length
        values are not an integers.
        """

        if not isinstance(val, int):
            raise InstrumentParameterException('Value %s is not an integer.' % str(val))
        elif not isinstance(slen, int):
            raise InstrumentParameterException('Value %s is not an integer.' % str(slen))
        else:
            hexstr = format(val, 'X')
            return hexstr.zfill(slen)

    @staticmethod
    def _current_sami_time():
        """
        Create a GMT timestamp in seconds using January 1, 1904 as the Epoch
        @retval an integer value representing the number of seconds since
            January 1, 1904.
        """

        log.debug('SamiProtocol._current_sami_time')

        utcnow = datetime.datetime.utcnow()
        time.sleep((1e6 - utcnow.microsecond) / 1e6)
        utcnow = datetime.datetime.utcnow()

        delt = (utcnow - SAMI_EPOCH)
        sami_seconds_since_epoch = delt.total_seconds()

        return sami_seconds_since_epoch

    def _current_sami_time_hex_str(self):
        """
        Get current GMT time since SAMI epoch, January 1, 1904
        @retval an 8 character hex string representing the GMT number of seconds
        since January 1, 1904.
        """
        log.debug('SamiProtocol._current_sami_time_hex_str')

        sami_seconds_since_epoch = self._current_sami_time() + TIME_WAKEUP_DELAY
        sami_seconds_hex_string = format(int(sami_seconds_since_epoch), 'X')
        sami_seconds_hex_string = sami_seconds_hex_string.zfill(8)

        log.debug('SamiProtocol._current_sami_time_hex_str: sami_seconds_hex_string = ' + sami_seconds_hex_string)

        return sami_seconds_hex_string

    def _get_config_value_str(self, param):
        """
        Get parameter value from param dictionary
        @param param parameter value to get
        @retval parameter value string
        """
        value = self._param_dict.get(param)
        log.debug('SamiProtocol._get_config_value_str(): self._param_dict.get_config_value(param) = ' + param + ' = ' + str(value))
        value_str = self._param_dict.format(param, value)
        log.debug('SamiProtocol._get_config_value_str(): self._param_dict.format(param, value) = ' + param + ' = ' + value_str)

        return value_str

    @staticmethod
    def _add_config_str_padding(configuration_string):
        """
        Add padding to configuration string
        @param configuration_string
        @retval configuration_string
        """
        config_string_length_no_padding = len(configuration_string)
        log.debug('Protocol._add_config_str_padding(): config_string_length_no_padding = ' + str(config_string_length_no_padding))

        zero_padding_length = CONFIG_WITH_0_PADDING - config_string_length_no_padding
        zero_padding = '0' * zero_padding_length
        configuration_string += zero_padding
        config_string_length_0_padding = len(configuration_string)
        log.debug('Protocol._add_config_str_padding(): config_string_length_0_padding = ' + str(config_string_length_0_padding))

        f_padding_length = CONFIG_WITH_0_AND_F_PADDING - config_string_length_0_padding
        f_padding = 'F' * f_padding_length
        configuration_string += f_padding
        config_string_length_0_and_f_padding = len(configuration_string)
        log.debug('Protocol._add_config_str_padding(): config_string_length_0_and_f_padding = ' + str(config_string_length_0_and_f_padding))

        return configuration_string

    def _build_configuration_string_base(self, parameter_list, override_params_dict):
        """
        Build configuration string
        @param parameter_list list of parameters to set in string
        @param override_params_dict parameters to override in config string
        @retval configuration_string
        """
        configuration_string = ''

        # LAUNCH_TIME always set to current GMT sami time
        sami_time_hex_str = self._current_sami_time_hex_str()
        log.debug('Protocol._build_configuration_string_base(): LAUNCH TIME = ' + sami_time_hex_str)
        configuration_string += sami_time_hex_str

        for param in parameter_list:
            if param in override_params_dict:
                log.debug('Protocol._build_configuration_string_base(): Overriding param = ' +
                          str(param) + ' with ' +
                          str(override_params_dict[param]))
                config_value_hex_str = self._param_dict.format(param, override_params_dict[param])
            else:
                config_value_hex_str = self._get_config_value_str(param)

            configuration_string += config_value_hex_str

        config_string_length_no_padding = len(configuration_string)
        log.debug('Protocol._build_configuration_string_base(): config_string_length_no_padding = ' + str(config_string_length_no_padding))

        configuration_string = SamiProtocol._add_config_str_padding(configuration_string)

        return configuration_string


    ## Verify configuration and update parameter dictionary if the configuration string is set correctly
    def _verify_and_update_configuration(self, configuration_string):
        """
        Verify config string set correctly on instrument and update param dict
        @param configuration_string
        @raise InstrumentProtocolException Invalid configuration string
        """
        log.debug('Protocol._verify_and_update_configuration()')

        configuration_string_regex = self._get_configuration_string_regex_matcher()

        instrument_configuration_string = self._do_cmd_resp(SamiInstrumentCommand.GET_CONFIG, timeout=TIMEOUT, response_regex=configuration_string_regex)

        log.debug('SamiProtocol._verify_and_update_configuration: instrument_configuration_string = ' + instrument_configuration_string)

        # if configuration_string == instrument_configuration_string.strip(NEWLINE):
        if configuration_string == instrument_configuration_string:
            log.debug('Protocol._verify_and_update_configuration(): CONFIGURATION IS VALID')
        else:
            log.error('Protocol._verify_and_update_configuration(): CONFIGURATION IS INVALID')
            raise InstrumentProtocolException("Invalid Configuration String")

        old_config = self._param_dict.get_all()

        log.debug('SamiProtocol._verify_and_update_configuration: old_config = ' + str(old_config))

        self._param_dict.update(instrument_configuration_string + NEWLINE)

        new_config = self._param_dict.get_all()

        log.debug('SamiProtocol._verify_and_update_configuration: new_config = ' + str(new_config))

        ## Compare values here to send config change event
        if not dict_equal(old_config, new_config, ignore_keys=SamiParameter.LAUNCH_TIME):
            log.debug("Configuration has changed.")
            if (self.get_current_state() == SamiProtocolState.COMMAND):
                log.debug("Configuration has changed and in command state.  Send driver event.")
                self._driver_event(DriverAsyncEvent.CONFIG_CHANGE)
        else:
            log.debug("Configuration has not changed.")

    def _take_regular_sample(self):
        """
        Take a regular sample from instrument
        """
        log.debug('SamiProtocol._take_regular_sample(): _take_regular_sample() START')

        self._pre_sample_processing()

        start_time = time.time()

        ## An exception is raised if timeout is hit.
        self._do_cmd_resp(SamiInstrumentCommand.ACQUIRE_SAMPLE_SAMI, timeout = self._get_sample_timeout(), response_regex=self._get_sample_regex())

        sample_time = time.time() - start_time

        log.debug('Protocol._take_regular_sample(): Regular Sample took ' + str(sample_time) + ' to FINISH')

    def _check_for_engineering_parameters(self, params):
        """
        Remove engineering parameters from param dict and check if they have changed.  If there is a change,
        raise a CONFIG_CHANGE event.
        @param dict of parameters to check for engineering
        """
        for engineering_parameter in self._engineering_parameters:
            if engineering_parameter in params:
                old_value = self._param_dict.get(engineering_parameter)
                new_value = params.pop(engineering_parameter)
                log.debug('SamiProtocol.check_for_engineering_parameters(): %s old/new = %d/%d' %
                          (engineering_parameter , old_value, new_value))
                if new_value != old_value:
                    self._param_dict.set_value(engineering_parameter,
                                               new_value)
                    log.debug('SamiProtocol.check_for_engineering_parameters(): Updated %s' % engineering_parameter)
                    self._driver_event(DriverAsyncEvent.CONFIG_CHANGE)
                else:
                    log.debug('SamiProtocol.check_for_engineering_parameters(): %s not updated' % engineering_parameter)

                log.debug('SamiProtocol.check_for_engineering_parameters(): %s = %s' %
                          (engineering_parameter, str(self._param_dict.get(engineering_parameter))))

                log.debug('SamiProtocol.check_for_engineering_parameters(): Removed %s, params = %s' %
                          (engineering_parameter, str(params)))

    def _verify_checksum(self, chunk, matcher):
        """
        Verify checksum of sample returned from instrument
        @param chunk returned from instrument
        @param matcher regular expression to match sample
        @raise SampleException check sum is invalid
        """
        matched = matcher.match(chunk)
        record_type = matched.group(3)
        log.debug('Protocol.verify_checksum(): sample record_type = ' + record_type)
        log.debug('Protocol.verify_checksum(): sample chunk = ' + chunk)

        ## Remove any whitespace
        sample_string = chunk.rstrip()
        checksum = sample_string[-2:]
        checksum_int = int(checksum, 16)
        log.debug('Checksum = %s hex, %d dec' % (checksum, checksum_int))
        calculated_checksum_string = sample_string[3:-2]
        log.debug('Checksum String = %s' % calculated_checksum_string)
        calculated_checksum = self.calc_crc(calculated_checksum_string)
        log.debug('Checksum/Calculated Checksum = %d/%d' % (checksum_int,calculated_checksum))

        if checksum_int != calculated_checksum:
            log.error("Sample Check Sum Invalid %d/%d, throwing exception." % (checksum_int,calculated_checksum))
            raise SampleException("Sample Check Sum Invalid %d/%d" % (checksum_int,calculated_checksum))

    @staticmethod
    def calc_crc(s):
        """
        Compute a checksum for a Sami data record or control record string.

        The '*' (character 1) and unique identifying byte (byte 1,
        characters 2 & 3) at the beginning should be excluded from the
        checksum calculation as well as the checksum value itself (last
        byte, last 2 characters). It should include the record length
        (byte 2, characters 4 & 5).

        Note that this method does NOT calculate the checksum on the
        configuration string that is returned during instrument
        configuration.

        @author Chris Center
        @param s: string for check-sum analysis.
        """

        log.debug('SamiProtocol.calc_crc')

        # num_points: number of bytes (each byte is 2-chars).
        num_points = len(s)/2

        cs = 0
        k = 0
        for i in range(num_points):
            value = int(s[k:k+2], 16)  # 2-chars per data point
            cs = cs + value
            k = k + 2
        cs = cs & 0xFF
        return(cs)

    def _build_param_dict(self):
        """
        Build the parameter dictionary.  Overridden by device specific subclasses.
        """

        log.debug('SamiProtocol._build_param_dict()')

        self._param_dict = ProtocolParameterDict()

        configuration_string_regex = self._get_configuration_string_regex()

        # Add parameter handlers to parameter dict.

        self._param_dict.add(SamiParameter.LAUNCH_TIME, configuration_string_regex,
                             lambda match: int(match.group(1), 16),
                             lambda x: self._int_to_hexstring(x, 8),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=0x00000000,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='launch time')

        self._param_dict.add(SamiParameter.START_TIME_FROM_LAUNCH, configuration_string_regex,
                             lambda match: int(match.group(2), 16),
                             lambda x: self._int_to_hexstring(x, 8),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=FIVE_YEARS_IN_SECONDS,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='start time after launch time')

        self._param_dict.add(SamiParameter.STOP_TIME_FROM_START, configuration_string_regex,
                             lambda match: int(match.group(3), 16),
                             lambda x: self._int_to_hexstring(x, 8),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=ONE_YEAR_IN_SECONDS,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='stop time after start time')

        self._param_dict.add(SamiParameter.SAMI_SAMPLE_INTERVAL, configuration_string_regex,
                             lambda match: int(match.group(5), 16),
                             lambda x: self._int_to_hexstring(x, 6),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=0x000E10,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='sami sample interval')

        self._param_dict.add(SamiParameter.SAMI_PARAMS_POINTER, configuration_string_regex,
                             lambda match: int(match.group(7), 16),
                             lambda x: self._int_to_hexstring(x, 2),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=0x02,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='sami parameter pointer')

        self._param_dict.add(SamiParameter.DEVICE2_SAMPLE_INTERVAL, configuration_string_regex,
                             lambda match: int(match.group(11), 16),
                             lambda x: self._int_to_hexstring(x, 6),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=0x000000,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='device 2 sample interval')

        self._param_dict.add(SamiParameter.DEVICE2_DRIVER_VERSION, configuration_string_regex,
                             lambda match: int(match.group(12), 16),
                             lambda x: self._int_to_hexstring(x, 2),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=0x00,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='device 2 driver version')

        self._param_dict.add(SamiParameter.DEVICE2_PARAMS_POINTER, configuration_string_regex,
                             lambda match: int(match.group(13), 16),
                             lambda x: self._int_to_hexstring(x, 2),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=0x00,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='device 2 parameter pointer')

        self._param_dict.add(SamiParameter.DEVICE3_SAMPLE_INTERVAL, configuration_string_regex,
                             lambda match: int(match.group(14), 16),
                             lambda x: self._int_to_hexstring(x, 6),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=0x000000,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='device 3 sample interval')

        self._param_dict.add(SamiParameter.DEVICE3_DRIVER_VERSION, configuration_string_regex,
                             lambda match: int(match.group(15), 16),
                             lambda x: self._int_to_hexstring(x, 2),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=0x00,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='device 3 driver version')

        self._param_dict.add(SamiParameter.DEVICE3_PARAMS_POINTER, configuration_string_regex,
                             lambda match: int(match.group(16), 16),
                             lambda x: self._int_to_hexstring(x, 2),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=0x00,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='device 3 parameter pointer')

        self._param_dict.add(SamiParameter.PRESTART_SAMPLE_INTERVAL, configuration_string_regex,
                             lambda match: int(match.group(17), 16),
                             lambda x: self._int_to_hexstring(x, 6),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=0x000000,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='prestart sample interval')

        self._param_dict.add(SamiParameter.PRESTART_DRIVER_VERSION, configuration_string_regex,
                             lambda match: int(match.group(18), 16),
                             lambda x: self._int_to_hexstring(x, 2),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=0x00,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='prestart driver version')

        ## Changed from 0x0D to 0x00 since there is not external device
        self._param_dict.add(SamiParameter.PRESTART_PARAMS_POINTER, configuration_string_regex,
                             lambda match: int(match.group(19), 16),
                             lambda x: self._int_to_hexstring(x, 2),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=0x00,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='prestart parameter pointer')

        # Changed from invalid value 0x00 to 0x07 setting bits, (2) Send live records, (1) Send ^(record type),
        #   (0) 57600 serial port
        self._param_dict.add(SamiParameter.GLOBAL_CONFIGURATION, configuration_string_regex,
                             lambda match: int(match.group(20), 16),
                             lambda x: self._int_to_hexstring(x, 2),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=True,
                             default_value=0x07,
                             visibility=ParameterDictVisibility.READ_ONLY,
                             display_name='global bits (set to 00000111)')

        ## Engineering parameter to set pseudo auto sample rate, set as startup parameter because it is configurable
        ##   by the user and should be reapplied on application of startup parameters.
        self._param_dict.add(SamiParameter.AUTO_SAMPLE_INTERVAL, r'Auto sample rate = ([0-9]+)',
                             lambda match: match.group(1),
                             lambda x: int(x),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=False,
                             default_value=3600,
                             visibility=ParameterDictVisibility.READ_WRITE,
                             display_name='auto sample interval')

        self._param_dict.add(SamiParameter.FLUSH_DURATION, r'Flush duration = ([0-9]+)',
                             lambda match: match.group(1),
                             lambda x: self._int_to_hexstring(x, 2),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=False,
                             default_value=0x8,
                             visibility=ParameterDictVisibility.READ_WRITE,
                             display_name='flush duration')

        self._param_dict.add(SamiParameter.PUMP_100ML_CYCLES, r'Pump 100ml cycles = ([0-9]+)',
                             lambda match: match.group(1),
                             lambda x: self._int_to_hexstring(x, 2),
                             type=ParameterDictType.INT,
                             startup_param=True,
                             direct_access=False,
                             default_value=0x1,
                             visibility=ParameterDictVisibility.READ_WRITE,
                             display_name='pump 100ml cycles')

    def _pre_sample_processing(self):
        """
        Processing before taking a sample.  Override in sub class if needed
        """

        log.debug('Protocol._pre_sample_processing(): None')

    def _got_chunk(self, chunk, timestamp):
        """
        Received matched data from chunker. Overridden by device specific subclasses.
        """

        raise NotImplementedException()

    def _get_specific_configuration_string_parameters(self):
        """
        Get list of instrument specific parameters.  Overridden by device specific subclasses.
        """

        raise NotImplementedException()

    def _get_configuration_string_regex(self):
        """
        Get config string regular expression.  Overridden by device specific subclasses.
        """

        raise NotImplementedException()

    def _get_configuration_string_regex_matcher(self):
        """
        Get config string regular expression matcher.  Overridden by device specific subclasses.
        """

        raise NotImplementedException()

    def _get_sample_regex(self):
        """
        Get sample regular expression.  Overridden by device specific subclasses.
        """

        raise NotImplementedException()

    def _get_sample_timeout(self):
        """
        Get timeout for sample.  Overridden by device specific subclasses.
        """

        raise NotImplementedException()

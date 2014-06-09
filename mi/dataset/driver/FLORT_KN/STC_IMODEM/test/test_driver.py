"""
@package mi.dataset.driver.FLORT_KN.STC_IMODEM.test.test_driver
@file marine-integrations/mi/dataset/driver/FLORT_KN/STC_IMODEM/driver.py
@author Emily Hahn
@brief Test cases for FLORT_KN__STC_IMODEM driver

USAGE:
 Make tests verbose and provide stdout
   * From the IDK
       $ bin/dsa/test_driver
       $ bin/dsa/test_driver -i [-t testname]
       $ bin/dsa/test_driver -q [-t testname]
"""

__author__ = 'Emily Hahn'
__license__ = 'Apache 2.0'

import unittest

from nose.plugins.attrib import attr
from mock import Mock

from mi.core.log import get_logger ; log = get_logger()
from mi.idk.exceptions import SampleTimeout

from mi.idk.dataset.unit_test import DataSetTestCase
from mi.idk.dataset.unit_test import DataSetIntegrationTestCase
from mi.idk.dataset.unit_test import DataSetQualificationTestCase
from mi.dataset.dataset_driver import DataSourceConfigKey, DataSetDriverConfigKeys
from mi.dataset.dataset_driver import DriverParameter

from mi.dataset.driver.FLORT_KN.STC_IMODEM.driver import \
    FLORT_KN__STC_IMODEN_DataSetDriver, \
    DataTypeKey
from mi.dataset.parser.flort_kn__stc_imodem import \
    Flort_kn__stc_imodemParserDataParticle, \
    Flort_kn__stc_imodemParserDataParticleRecovered, \
    DataParticleType

from pyon.agent.agent import ResourceAgentState
from interface.objects import ResourceAgentErrorEvent

DIR_FLORT_KN_LIVE = '/tmp/dsatest1'
DIR_FLORT_KN_RECOVERED = '/tmp/dsatest2'

# Fill in driver details
DataSetTestCase.initialize(
    driver_module='mi.dataset.driver.FLORT_KN.STC_IMODEM.driver',
    driver_class='FLORT_KN__STC_IMODEM_DataSetDriver',
    agent_resource_id = '123xyz',
    agent_name = 'Agent007',
    agent_packet_config = FLORT_KN__STC_IMODEN_DataSetDriver.stream_config(),
    startup_config = {
        DataSourceConfigKey.RESOURCE_ID: 'flort_kn__stc_imodem',
        DataSourceConfigKey.HARVESTER:
        {
            DataTypeKey.FLORT_KN_INSTRUMENT:
            {
                DataSetDriverConfigKeys.DIRECTORY: DIR_FLORT_KN_LIVE,
                DataSetDriverConfigKeys.PATTERN: 'E*.DAT',
                DataSetDriverConfigKeys.FREQUENCY: 1,
            },
            DataTypeKey.FLORT_KN_RECOVERED:
            {
                DataSetDriverConfigKeys.DIRECTORY: DIR_FLORT_KN_RECOVERED,
                DataSetDriverConfigKeys.PATTERN: 'E*.DAT',
                DataSetDriverConfigKeys.FREQUENCY: 1,
            }
        },
        DataSourceConfigKey.PARSER: {
            DataTypeKey.FLORT_KN_INSTRUMENT: {},
            DataTypeKey.FLORT_KN_RECOVERED: {}
        }
    }
)



###############################################################################
#                            INTEGRATION TESTS                                #
# Device specific integration tests are for                                   #
# testing device specific capabilities                                        #
###############################################################################
@attr('INT', group='mi')
class IntegrationTest(DataSetIntegrationTestCase):
 
    def test_get(self):
        """
        Test that we can get data from files.  Verify that the driver
        sampling can be started and stopped
        """
        self.clear_sample_data()

        # Start sampling and watch for an exception
        self.driver.start_sampling()

        self.clear_async_data()
        self.create_sample_data_set_dir('first.DAT', DIR_FLORT_KN_LIVE, "E0000001.DAT")
        self.assert_data(Flort_kn__stc_imodemParserDataParticle, 'first.result.yml', count=1, timeout=10)

        self.clear_async_data()
        self.create_sample_data_set_dir('second.DAT', DIR_FLORT_KN_LIVE, "E0000002.DAT")
        self.assert_data(Flort_kn__stc_imodemParserDataParticle, 'second.result.yml', count=4, timeout=10)

        self.clear_async_data()
        self.create_sample_data_set_dir('E0000303.DAT', DIR_FLORT_KN_LIVE, "E0000303.DAT")
        # start is the same particle here, just use the same results
        self.assert_data(Flort_kn__stc_imodemParserDataParticle, count=32, timeout=10)

        self.clear_async_data()
        self.create_sample_data_set_dir('first.DAT', DIR_FLORT_KN_RECOVERED, "E0000001.DAT")
        self.assert_data(Flort_kn__stc_imodemParserDataParticleRecovered, 'first.result.yml', count=1, timeout=10)

        self.clear_async_data()
        self.create_sample_data_set_dir('second.DAT', DIR_FLORT_KN_RECOVERED, "E0000002.DAT")
        self.assert_data(Flort_kn__stc_imodemParserDataParticleRecovered, 'second.result.yml', count=4, timeout=10)

        self.clear_async_data()
        self.create_sample_data_set_dir('E0000303.DAT', DIR_FLORT_KN_RECOVERED, "E0000303.DAT")
        # start is the same particle here, just use the same results
        self.assert_data(Flort_kn__stc_imodemParserDataParticleRecovered, count=32, timeout=10)

    def test_stop_resume(self):
        """
        Test the ability to stop and restart the process
        """
        path_1 = self.create_sample_data_set_dir('first.DAT', DIR_FLORT_KN_LIVE, "E0000001.DAT")
        path_2 = self.create_sample_data_set_dir('second.DAT', DIR_FLORT_KN_LIVE, "E0000002.DAT")
        path_3 = self.create_sample_data_set_dir('first.DAT', DIR_FLORT_KN_RECOVERED, "E0000001.DAT")
        path_4 = self.create_sample_data_set_dir('second.DAT', DIR_FLORT_KN_RECOVERED, "E0000002.DAT")

        # Create and store the new driver state
        state = {DataTypeKey.FLORT_KN_INSTRUMENT:
                     {'E0000001.DAT': self.get_file_state(path_1, True, 50),
                      'E0000002.DAT': self.get_file_state(path_2, False, 76)},
                 DataTypeKey.FLORT_KN_RECOVERED:
                     {'E0000001.DAT': self.get_file_state(path_3, True, 50),
                      'E0000002.DAT': self.get_file_state(path_4, False, 76)},
                 }
        self.driver = self._get_driver_object(memento=state)

        # create some data to parse
        self.clear_async_data()

        self.driver.start_sampling()

        # verify data is produced
        self.assert_data(Flort_kn__stc_imodemParserDataParticle, 'partial_second.result.yml', count=2, timeout=10)
        self.assert_data(Flort_kn__stc_imodemParserDataParticleRecovered, 'partial_second_recovered.result.yml',
                         count=2, timeout=10)

    def test_stop_start_ingest(self):
        """
        Test the ability to stop and restart sampling, and ingesting files in the correct order
        """
        # create some data to parse
        self.clear_async_data()

        self.driver.start_sampling()

        self.create_sample_data_set_dir('first.DAT', DIR_FLORT_KN_LIVE, "E0000001.DAT")
        self.create_sample_data_set_dir('second.DAT', DIR_FLORT_KN_LIVE, "E0000002.DAT")
        self.create_sample_data_set_dir('first.DAT', DIR_FLORT_KN_RECOVERED, "E0000001.DAT")
        self.create_sample_data_set_dir('second.DAT', DIR_FLORT_KN_RECOVERED, "E0000002.DAT")
        self.assert_data(Flort_kn__stc_imodemParserDataParticle, 'first.result.yml', count=1, timeout=10)
        self.assert_data(Flort_kn__stc_imodemParserDataParticleRecovered, 'first.result.yml', count=1, timeout=10)
        self.assert_file_ingested("E0000001.DAT")
        self.assert_file_not_ingested("E0000002.DAT")

        self.driver.stop_sampling()
        self.driver.start_sampling()

        self.assert_data(Flort_kn__stc_imodemParserDataParticle, 'second.result.yml', count=4, timeout=10)
        self.assert_file_ingested("E0000002.DAT")

    def test_get_any_order(self):
        """
Test that we can get data from files for all harvesters / parsers.
"""
        log.info("=========== START INTEG TEST GET ANY ORDER ================")

        # Start sampling.
        self.clear_sample_data()
        self.driver.start_sampling()

        log.info("LA101636_20.PD0 placed in Live directory")
        self.clear_async_data()
        self.create_sample_data_set_dir(
            'LA101636_20.PD0', DIR_FLORT_KN_LIVE)

        log.info("LB180210_50.PD0 placed in Recovered directory")
        self.clear_async_data()
        self.create_sample_data_set_dir(
            'LB180210_50.PD0', DIR_FLORT_KN_RECOVERED)

        # get the first 5 particles from the live directory
        self.assert_data(Flort_kn__stc_imodemParserDataParticle, 'LA101636_20_1_5.yml',
                         count=5, timeout=10)

        # get the first 12 particles from the recovered directory
        self.assert_data(Flort_kn__stc_imodemParserDataParticleRecovered, 'LB180210_50_1_12.yml',
                         count=12, timeout=10)

        # get the next 15 particles from the live directory
        self.assert_data(Flort_kn__stc_imodemParserDataParticle, 'LA101636_20_6_20.yml',
                         count=15, timeout=10)

        # get the next 8 particles from the recovered directory
        self.assert_data(Flort_kn__stc_imodemParserDataParticleRecovered, 'LB180210_50_13_20.yml',
                         count=8, timeout=10)


    def test_sample_exception(self):
        """
        test that a file is marked as parsed if it has a sample exception (which will happen with an empty file)
        """
        self.clear_async_data()

        config = self._driver_config()['startup_config']['harvester']['pattern']
        filename = config.replace("*", "foo")
        self.create_sample_data_set_dir(filename, DIR_FLORT_KN_RECOVERED)
        self.create_sample_data_set_dir(filename, DIR_FLORT_KN_LIVE)

        # Start sampling and watch for an exception
        self.driver.start_sampling()
        # an event catches the sample exception
        self.assert_event('ResourceAgentErrorEvent')
        self.assert_file_ingested(filename)

###############################################################################
#                            QUALIFICATION TESTS                              #
# Device specific qualification tests are for                                 #
# testing device specific capabilities                                        #
###############################################################################
@attr('QUAL', group='mi')
class QualificationTest(DataSetQualificationTestCase):
    def setUp(self):
        super(QualificationTest, self).setUp()

    def test_publish_path(self):
        """
        Setup an agent/driver/harvester/parser and verify that data is
        published out the agent
        """
        self.create_sample_data_set_dir('second.DAT', DIR_FLORT_KN_LIVE, 'E0000001.DAT')
        self.create_sample_data_set_dir('secondRecovered.DAT', DIR_FLORT_KN_RECOVERED, 'E0000001.DAT')
        self.assert_initialize(final_state=ResourceAgentState.COMMAND)

        # NOTE: If the processing is not slowed down here, the engineering samples are
        # returned in the wrong order
        self.dataset_agent_client.set_resource({DriverParameter.RECORDS_PER_SECOND: 1})
        self.assert_start_sampling()

        # Verify we get one sample
        try:
            resulta = self.data_subscribers.get_samples(DataParticleType.FLORT_KN_INS, 4)
            resultb = self.data_subscribers.get_samples(DataParticleType.HOLD_PLACE, 4)
            log.debug("RESULT: %s", resulta)
            log.debug("RESULT: %s", resultb)

            # Verify values
            self.assert_data_values(resulta, 'second.result.yml')
            self.assert_data_values(resultb, 'secondRecovered.result.yml')
        except Exception as e:
            log.error("Exception trapped: %s", e)
            self.fail("Sample timeout.")

    def test_large_import(self):
        """
        Test importing a large number of samples from the file at once
        """
        self.create_sample_data_set_dir('E0000303.DAT', DIR_FLORT_KN_LIVE)
        self.create_sample_data_set_dir('E0000427.DAT',  DIR_FLORT_KN_LIVE)
        self.create_sample_data_set_dir('E0000303.DAT', DIR_FLORT_KN_RECOVERED)
        self.create_sample_data_set_dir('E0000427.DAT',  DIR_FLORT_KN_RECOVERED)
        self.assert_initialize()

        # get results for each of the data particle streams
        self.data_subscribers.get_samples(DataParticleType.FLORT_KN_INS,64,40)
        self.data_subscribers.get_samples(DataParticleType.HOLD_PLACE,64,40)

    def test_status_in_middle(self):
        """
        This file has status particles in the middle and at the end
        """
        self.create_sample_data('E0000039.DAT')
        self.assert_initialize()

        # get results for each of the data particle streams
        self.get_samples(DataParticleType.FLORT_KN_INS, 53, 40)
        self.get_samples(DataParticleType.HOLD_PLACE, 53, 40)

    def test_stop_start(self):
        """
        Test the agents ability to start data flowing, stop, then restart
        at the correct spot.
        """
        log.info("CONFIG: %s", self._agent_config())
        self.create_sample_data_set_dir('first.DAT',  DIR_FLORT_KN_LIVE, "E0000001.DAT")
        self.create_sample_data_set_dir('first.DAT',  DIR_FLORT_KN_RECOVERED, "E0000001.DAT")

        self.assert_initialize(final_state=ResourceAgentState.COMMAND)

        # Slow down processing to 1 per second to give us time to stop
        self.dataset_agent_client.set_resource({DriverParameter.RECORDS_PER_SECOND: 1})
        self.assert_start_sampling()

        # Verify we get one sample
        try:
            # Read the first file and verify the data
            resulta = self.get_samples(DataParticleType.FLORT_KN_INS, 53, 40)
            resultb = self.get_samples(DataParticleType.HOLD_PLACE, 53, 40)
            log.debug("RESULT: %s", resulta)
            log.debug("RESULT: %s", resultb)

            # Verify values
            self.assert_data_values(resulta, DIR_FLORT_KN_LIVE, 'first.result.yml')
            self.assert_data_values(resultb, DIR_FLORT_KN_RECOVERED, 'firstRecovered.result.yml')
            self.assert_sample_queue_size(DataParticleType.FLORT_KN_INS, 0)
            self.assert_sample_queue_size(DataParticleType.HOLD_PLACE, 0)

            self.create_sample_data_set_dir('second.DAT', "E0000002.DAT")
            self.create_sample_data_set_dir('second.DAT', "E0000002.DAT")
            # Now read the first two records of the second file then stop
            resulta = self.get_samples(DataParticleType.FLORT_KN_INS, 2)
            resultb = self.get_samples(DataParticleType.HOLD_PLACE, 2)
            log.debug("got result 1 %s", resulta)
            log.debug("got result 1 %s", resultb)
            self.assert_stop_sampling()
            self.assert_sample_queue_size(DataParticleType.FLORT_KN_INS, 0)
            self.assert_sample_queue_size(DataParticleType.HOLD_PLACE, 0)

            # Restart sampling and ensure we get the last 5 records of the file
            self.assert_start_sampling()
            result2a = self.get_samples(DataParticleType.FLORT_KN_INS, 2)
            result2b = self.get_samples(DataParticleType.HOLD_PLACE, 2)
            log.debug("got result 2 %s", result2a)
            resulta.extend(result2a)
            resultb.extend(result2b)
            self.assert_data_values(resulta, 'second.result.yml')
            self.assert_data_values(resultb, 'secondRecovered.result.yml')

            self.assert_sample_queue_size(DataParticleType.FLORT_KN_INS, 0)
            self.assert_sample_queue_size(DataParticleType.HOLD_PLACE, 0)
        except SampleTimeout as e:
            log.error("Exception trapped: %s", e, exc_info=True)
            self.fail("Sample timeout.")

    def test_shutdown_restart(self):
        """
        Test the agents ability to start data flowing, stop, then restart
        at the correct spot.
        """
        log.info("CONFIG: %s", self._agent_config())
        self.create_sample_data_set_dir('first.DAT', DIR_FLORT_KN_LIVE, "E0000001.DAT")
        self.create_sample_data_set_dir('first.DAT', DIR_FLORT_KN_RECOVERED, "E0000001.DAT")

        self.assert_initialize(final_state=ResourceAgentState.COMMAND)

        # Slow down processing to 1 per second to give us time to stop
        self.dataset_agent_client.set_resource({DriverParameter.RECORDS_PER_SECOND: 1})
        self.assert_start_sampling()

        # Verify we get one sample
        try:
            # Read the first file and verify the data
            resulta = self.get_samples(DataParticleType.FLORT_KN_INS)
            resultb = self.get_samples(DataParticleType.HOLD_PLACE)
            log.debug("RESULT: %s", resulta)
            log.debug("RESULT: %s", resultb)

            # Verify values
            self.assert_data_values(resulta, 'first.result.yml')
            self.assert_data_values(resultb, 'firstRecovered.result.yml')
            self.assert_sample_queue_size(DataParticleType.FLORT_KN_INS, 0)
            self.assert_sample_queue_size(DataParticleType.HOLD_PLACE, 0)

            self.create_sample_data_set_dir('second.DAT', DIR_FLORT_KN_LIVE,  "E0000002.DAT")
            self.create_sample_data_set_dir('second.DAT', DIR_FLORT_KN_RECOVERED,  "E0000002.DAT")
            # Now read the first two records of the second file then stop
            resulta = self.get_samples(DataParticleType.FLORT_KN_INS, 2)
            resultb = self.get_samples(DataParticleType.HOLD_PLACE, 2)
            log.debug("RESULT: %s", resulta)
            log.debug("RESULT: %s", resultb)
            self.assert_stop_sampling()
            self.assert_sample_queue_size(DataParticleType.FLORT_KN_INS, 0)
            self.assert_sample_queue_size(DataParticleType.HOLD_PLACE, 0)

            # stop the agent
            self.stop_dataset_agent_client()
            # re-start the agent
            self.init_dataset_agent_client()
            #re-initialize
            self.assert_initialize(final_state=ResourceAgentState.COMMAND)
            # Restart sampling and ensure we get the last 2 records of the file
            self.assert_start_sampling()
            
            result2a = self.get_samples(DataParticleType.FLORT_KN_INS, 2)
            result2b = self.get_samples(DataParticleType.HOLD_PLACE, 2)
            log.debug("got result 2 %s", result2a)
            resulta.extend(result2a)
            log.debug("got result 2 %s", result2b)
            resultb.extend(result2b)
            self.assert_data_values(resulta, 'second.result.yml')
            self.assert_data_values(resultb, 'secondRecovered.result.yml')
            self.assert_sample_queue_size(DataParticleType.FLORT_KN_INS, 0)
            self.assert_sample_queue_size(DataParticleType.HOLD_PLACE, 0)
        except SampleTimeout as e:
            log.error("Exception trapped: %s", e, exc_info=True)
            self.fail("Sample timeout.")

    def test_parser_exception(self):
        """
        Test an exception is raised after the driver is started during
        record parsing.
        """
        self.clear_sample_data()
        self.create_sample_data_set_dir('bad.DAT', DIR_FLORT_KN_LIVE, 'E0000001.DAT')
        self.create_sample_data_set_dir('first.DAT', DIR_FLORT_KN_LIVE, 'E0000002.DAT')
        self.create_sample_data_set_dir('bad.DAT', DIR_FLORT_KN_RECOVERED, 'E0000001.DAT')
        self.create_sample_data_set_dir('first.DAT', DIR_FLORT_KN_RECOVERED, 'E0000002.DAT')
        self.assert_initialize()

        self.event_subscribers.clear_events()
        resulta = self.get_samples(DataParticleType.FLORT_KN_INS, 1)
        resultb = self.get_samples(DataParticleType.HOLD_PLACE, 1)
        self.assert_data_values(resulta, 'first.result.yml')
        self.assert_data_values(resultb, 'firstRecovered.result.yml')
        self.assert_sample_queue_size(DataParticleType.FLORT_KN_INS, 0)
        self.assert_sample_queue_size(DataParticleType.HOLD_PLACE, 0)

        # Verify an event was raised and we are in our retry state
        self.assert_event_received(ResourceAgentErrorEvent, 10)
        self.assert_state_change(ResourceAgentState.STREAMING, 10)


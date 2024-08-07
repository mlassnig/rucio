# Copyright European Organization for Nuclear Research (CERN) since 2012
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

'''
Dynamic data placement daemon.
'''

import logging
from datetime import datetime
from hashlib import md5
from json import dumps
from queue import Queue
from threading import Event, Thread
from time import sleep
from typing import TYPE_CHECKING, Optional
from uuid import uuid4

from requests import post
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException

import rucio.db.sqla.util
from rucio.client import Client
from rucio.common import exception
from rucio.common.config import config_get, config_get_options
from rucio.common.logging import setup_logging
from rucio.common.types import InternalScope
from rucio.daemons.c3po.collectors.free_space import FreeSpaceCollector
from rucio.daemons.c3po.collectors.jedi_did import JediDIDCollector
from rucio.daemons.c3po.collectors.workload import WorkloadCollector

if TYPE_CHECKING:
    from types import FrameType

GRACEFUL_STOP = Event()
DAEMON_NAME = 'c3po'


def read_free_space(
        once: bool = False,
        thread: int = 0,
        waiting_time: int = 1800,
        sleep_time: int = 10
) -> None:
    """
    Thread to collect the space usage information for RSEs.
    """
    free_space_collector = FreeSpaceCollector()
    timer = waiting_time
    while not GRACEFUL_STOP.is_set():
        if timer < waiting_time:
            timer += sleep_time
            sleep(sleep_time)
            continue

        logging.info('collecting free space')
        free_space_collector.collect_free_space()
        timer = 0


def read_workload(
        once: bool = False,
        thread: int = 0,
        waiting_time: int = 1800,
        sleep_time: int = 10
) -> None:
    """
    Thread to collect the workload information from PanDA.
    """
    workload_collector = WorkloadCollector()
    timer = waiting_time
    while not GRACEFUL_STOP.is_set():
        if timer < waiting_time:
            timer += sleep_time
            sleep(sleep_time)
            continue

        logging.info('collecting workload')
        workload_collector.collect_workload()
        timer = 0


def print_workload(
        once: bool = False,
        thread: int = 0,
        waiting_time: int = 600,
        sleep_time: int = 10
) -> None:
    """
    Thread to regularly output the workload to logs for debugging.
    """
    workload_collector = WorkloadCollector()
    timer = waiting_time
    while not GRACEFUL_STOP.is_set():
        if timer < waiting_time:
            timer += sleep_time
            sleep(sleep_time)
            continue

        logging.info('Number of sites cached %d' % len(workload_collector.get_sites()))
        for site in workload_collector.get_sites():
            logging.info('%s: %d / %d / %d' % (site, workload_collector.get_cur_jobs(site), workload_collector.get_avg_jobs(site), workload_collector.get_max_jobs(site)))
        timer = 0


def read_dids(
        once: bool = False,
        thread: int = 0,
        did_collector: Optional[JediDIDCollector] = None,
        waiting_time: int = 60,
        sleep_time: int = 10
) -> None:
    """
    Thread to collect DIDs for the placement algorithm.
    """
    timer = waiting_time
    while not GRACEFUL_STOP.is_set():
        if timer < waiting_time:
            timer += sleep_time
            sleep(sleep_time)
            continue

        if did_collector is not None:
            did_collector.get_dids()
        timer = 0


def add_rule(
        client: Client,
        did: dict[str, str],
        src_rse: str,
        dst_rse: str
) -> None:
    logging.debug('add rule for %s from %s to %s' % (did, src_rse, dst_rse))
    r = client.add_replication_rule([did, ], 1, dst_rse, lifetime=604800, account='c3po', source_replica_expression=src_rse, activity='Data Brokering', asynchronous=True)
    logging.debug(r)


def place_replica(
        did_queue: Queue,
        once: bool = False,
        thread: int = 0,
        waiting_time: int = 100,
        dry_run: bool = False,
        sampling: bool = False,
        algorithms: str = 't2_free_space_only_pop_with_network',
        datatypes: str = 'NTUP,DAOD',
        dest_rse_expr: str = 'type=DATADISK',
        max_bytes_hour: int = 100000000000000,
        max_files_hour: int = 100000,
        max_bytes_hour_rse: int = 50000000000000,
        max_files_hour_rse: int = 10000,
        min_popularity: int = 8,
        min_recent_requests: int = 5,
        max_replicas: int = 5,
        sleep_time: int = 10
) -> None:
    """
    Thread to run the placement algorithm to decide if and where to put new replicas.
    """
    try:
        c3po_options = config_get_options('c3po')
        client = None

        if 'algorithms' in c3po_options:
            algorithms = config_get('c3po', 'algorithms')

        algorithms_list = algorithms.split(',')

        if not dry_run:
            if len(algorithms_list) != 1:
                logging.error('Multiple algorithms are only allowed in dry_run mode')
                return
            client = Client(auth_type='x509_proxy', account='c3po', creds={'client_proxy': '/opt/rucio/etc/ddmadmin.long.proxy'})

        vo = client.vo
        instances = {}
        for algorithm in algorithms_list:
            module_path = 'rucio.daemons.c3po.algorithms.' + algorithm
            module = __import__(module_path, globals(), locals(), ['PlacementAlgorithm'])
            instance = module.PlacementAlgorithm(datatypes, dest_rse_expr, max_bytes_hour, max_files_hour, max_bytes_hour_rse, max_files_hour_rse, min_popularity, min_recent_requests, max_replicas)
            instances[algorithm] = instance

        params = {
            'dry_run': dry_run,
            'sampling': sampling,
            'datatypes': datatypes,
            'dest_rse_expr': dest_rse_expr,
            'max_bytes_hour': max_bytes_hour,
            'max_files_hour': max_files_hour,
            'max_bytes_hour_rse': max_bytes_hour_rse,
            'max_files_hour_rse': max_files_hour_rse,
            'min_recent_requests': min_recent_requests,
            'min_popularity': min_popularity
        }

        instance_id = str(uuid4()).split('-')[0]

        elastic_url = config_get('c3po', 'elastic_url')
        elastic_index = config_get('c3po', 'elastic_index')

        ca_cert = False
        if 'ca_cert' in c3po_options:
            ca_cert = config_get('c3po', 'ca_cert')

        auth = False
        if ('elastic_user' in c3po_options) and ('elastic_pass' in c3po_options):
            auth = HTTPBasicAuth(config_get('c3po', 'elastic_user'), config_get('c3po', 'elastic_pass'))

        w = waiting_time
        while not GRACEFUL_STOP.is_set():
            if w < waiting_time:
                w += sleep_time
                sleep(sleep_time)
                continue
            len_dids = did_queue.qsize()

            if len_dids > 0:
                logging.debug('(%s) %d did(s) in queue' % (instance_id, len_dids))
            else:
                logging.debug('(%s) no dids in queue' % (instance_id))

            for _ in range(0, len_dids):
                did = did_queue.get()
                if isinstance(did[0], str):
                    did[0] = InternalScope(did[0], vo=vo)
                for algorithm, instance in instances.items():
                    logging.info('(%s:%s) Retrieved %s:%s from queue. Run placement algorithm' % (algorithm, instance_id, did[0], did[1]))
                    decision = instance.place(did)
                    decision['@timestamp'] = datetime.utcnow().isoformat()
                    decision['algorithm'] = algorithm
                    decision['instance_id'] = instance_id
                    decision['params'] = params

                    create_rule = True
                    if sampling and 'error_reason' not in decision:
                        create_rule = bool(ord(md5(decision['did']).hexdigest()[-1]) & 1)
                        decision['create_rule'] = create_rule
                    # write the output to ES for further analysis
                    index_url = elastic_url + '/' + elastic_index + '-' + datetime.utcnow().strftime('%Y-%m') + '/record/'
                    try:
                        if ca_cert:
                            r = post(index_url, data=dumps(decision), verify=ca_cert, auth=auth)
                        else:
                            r = post(index_url, data=dumps(decision))
                        if r.status_code != 201:
                            logging.error(r)
                            logging.error('(%s:%s) could not write to ElasticSearch' % (algorithm, instance_id))
                    except RequestException as e:
                        logging.error('(%s:%s) could not write to ElasticSearch' % (algorithm, instance_id))
                        logging.error(e)
                        continue

                    logging.debug(decision)
                    if 'error_reason' in decision:
                        logging.error('(%s:%s) The placement algorithm ran into an error: %s' % (algorithm, instance_id, decision['error_reason']))
                        continue

                    logging.info('(%s:%s) Decided to place a new replica for %s on %s' % (algorithm, instance_id, decision['did'], decision['destination_rse']))

                    if (not dry_run) and create_rule:
                        # DO IT!
                        try:
                            add_rule(client, {'scope': did[0].external, 'name': did[1]}, decision.get('source_rse'), decision.get('destination_rse'))  # type: ignore
                        except exception.RucioException as e:
                            logging.debug(e)

            w = 0
    except Exception as e:
        logging.critical(e)


def stop(signum: Optional[int] = None, frame: Optional['FrameType'] = None) -> None:
    """
    Graceful exit.
    """
    GRACEFUL_STOP.set()


def run(
        once: bool = False,
        threads: int = 1,
        only_workload: bool = False,
        dry_run: bool = False,
        sampling: bool = False,
        algorithms: str = 't2_free_space_only_pop_with_network',
        datatypes: str = 'NTUP,DAOD',
        dest_rse_expr: str = 'type=DATADISK',
        max_bytes_hour: int = 100000000000000,
        max_files_hour: int = 100000,
        max_bytes_hour_rse: int = 50000000000000,
        max_files_hour_rse: int = 10000,
        min_popularity: int = 8,
        min_recent_requests: int = 5,
        max_replicas: int = 5,
        waiting_time_read_free_space: int = 1800,
        waiting_time_read_workload: int = 1800,
        waiting_time_print_workload: int = 600,
        waiting_time_read_dids: int = 60,
        waiting_time_place_replica: int = 100,
        sleep_time: int = 10
) -> None:
    """
    Starts up the main thread
    """
    setup_logging(process_name=DAEMON_NAME)

    if rucio.db.sqla.util.is_old_db():
        raise exception.DatabaseException('Database was not updated, daemon won\'t start')

    logging.info('activating C-3PO')

    thread_list = []
    try:
        if only_workload:
            logging.info('running in workload-collector-only mode')
            thread_list.append(Thread(target=read_workload, name='read_workload', kwargs={'thread': 0,
                                                                                          'waiting_time': waiting_time_read_workload,
                                                                                          'sleep_time': sleep_time}))
            thread_list.append(Thread(target=print_workload, name='print_workload', kwargs={'thread': 0,
                                                                                            'waiting_time': waiting_time_print_workload,
                                                                                            'sleep_time': sleep_time}))
        else:
            logging.info('running in placement mode')
            did_queue = Queue()
            dc = JediDIDCollector(did_queue)

            thread_list.append(Thread(target=read_free_space, name='read_free_space', kwargs={'thread': 0,
                                                                                              'waiting_time': waiting_time_read_free_space,
                                                                                              'sleep_time': sleep_time}))
            thread_list.append(Thread(target=read_dids, name='read_dids', kwargs={'thread': 0,
                                                                                  'did_collector': dc,
                                                                                  'waiting_time': waiting_time_read_dids,
                                                                                  'sleep_time': sleep_time}))
            thread_list.append(Thread(target=place_replica, name='place_replica', kwargs={'thread': 0,
                                                                                          'did_queue': did_queue,
                                                                                          'waiting_time': waiting_time_place_replica,
                                                                                          'algorithms': algorithms,
                                                                                          'dry_run': dry_run,
                                                                                          'sampling': sampling,
                                                                                          'datatypes': datatypes,
                                                                                          'dest_rse_expr': dest_rse_expr,
                                                                                          'max_bytes_hour': max_bytes_hour,
                                                                                          'max_files_hour': max_files_hour,
                                                                                          'max_bytes_hour_rse': max_bytes_hour_rse,
                                                                                          'max_files_hour_rse': max_files_hour_rse,
                                                                                          'min_popularity': min_popularity,
                                                                                          'min_recent_requests': min_recent_requests,
                                                                                          'max_replicas': max_replicas,
                                                                                          'sleep_time': sleep_time}))

        for t in thread_list:
            t.start()

        logging.info('waiting for interrupts')

        while len(thread_list) > 0:
            [t.join(timeout=3) for t in thread_list if t and t.is_alive()]
    except Exception as error:
        logging.critical(error)

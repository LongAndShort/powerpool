import urllib3
import gevent
import socket
import time
import datetime

from cryptokit.rpc import CoinserverRPC, CoinRPCException
from collections import deque
from cryptokit.util import pack
from cryptokit.bitcoin import data as bitcoin_data
from gevent import sleep, Greenlet
from gevent.event import Event

from . import RPCException, NodeMonitorMixin, Jobmanager
from ..lib import loop, REQUIRED


class MonitorAuxNetwork(Jobmanager, NodeMonitorMixin):
    gl_methods = ['_monitor_nodes', '_check_new_jobs']
    one_min_stats = ['work_restarts', 'new_jobs']

    defaults = dict(enabled=False,
                    work_interval=1,
                    signal=None,
                    currency=REQUIRED,
                    coinserv=REQUIRED,
                    flush=False,
                    send=True)

    def __init__(self, config):
        self._configure(config)
        NodeMonitorMixin.__init__(self)

        self.new_job = Event()
        self.last_work = {'hash': None}
        self.block_stats = dict(accepts=0,
                                rejects=0,
                                solves=0,
                                last_solve_height=None,
                                last_solve_time=None,
                                last_solve_worker=None)
        self.current_net = dict(difficulty=None, height=None)
        self.recent_blocks = deque(maxlen=15)

        if self.config['signal']:
            self.logger.info("Listening for push block notifs on signal {}"
                             .format(self.config['signal']))
            gevent.signal(self.config['signal'], self.update, reason="Signal recieved")

    def found_block(self, address, worker, header, coinbase_raw, job, start):
        aux_data = job.merged_data[self.config['currency']]
        self.block_stats['solves'] += 1
        self.logger.info("New {} Aux block at height {}"
                         .format(self.config['currency'], aux_data['height']))
        aux_block = (
            pack.IntType(256, 'big').pack(aux_data['hash']).encode('hex'),
            bitcoin_data.aux_pow_type.pack(dict(
                merkle_tx=dict(
                    tx=bitcoin_data.tx_type.unpack(coinbase_raw),
                    block_hash=bitcoin_data.hash256(header),
                    merkle_link=job.merkle_link,
                ),
                merkle_link=bitcoin_data.calculate_merkle_link(aux_data['hashes'],
                                                               aux_data['index']),
                parent_block_header=bitcoin_data.block_header_type.unpack(header),
            )).encode('hex'),
        )

        retries = 0
        while retries < 5:
            retries += 1
            new_height = aux_data['height'] + 1
            res = False
            try:
                res = self.coinserv.getauxblock(*aux_block)
            except (CoinRPCException, socket.error, ValueError) as e:
                self.logger.error("{} Aux block failed to submit to the server!"
                                  .format(self.config['currency']), exc_info=True)
                self.logger.error(getattr(e, 'error'))

            if res is True:
                self.logger.info("NEW {} Aux BLOCK ACCEPTED!!!".format(self.config['currency']))
                # Record it for the stats
                self.block_stats['accepts'] += 1
                self.recent_blocks.append(
                    dict(height=new_height, timestamp=int(time.time())))

                # submit it to our reporter if configured to do so
                if self.config['send']:
                    self.logger.info("Submitting {} new block to reporter"
                                     .format(self.config['currency']))
                    # A horrible mess that grabs the required information for
                    # reporting the new block. Pretty failsafe so at least
                    # partial information will be reporter regardless
                    try:
                        hsh = self.coinserv.getblockhash(new_height)
                    except Exception:
                        self.logger.info("", exc_info=True)
                        hsh = ''
                    try:
                        block = self.coinserv.getblock(hsh)
                    except Exception:
                        self.logger.info("", exc_info=True)
                    try:
                        trans = self.coinserv.gettransaction(block['tx'][0])
                        amount = trans['details'][0]['amount']
                    except Exception:
                        self.logger.info("", exc_info=True)
                        amount = -1

                    self.block_stats['last_solve_hash'] = hsh
                    return dict(address=address,
                                height=new_height,
                                total_subsidy=int(amount * 100000000),
                                fees=-1,
                                hex_bits="%0.6X" % bitcoin_data.FloatingInteger.from_target_upper_bound(aux_data['target']).bits,
                                hash=hsh,
                                merged=self.config['currency'],
                                algo=self.config['algo'],
                                worker=worker)

                break  # break retry loop if success
            else:
                self.logger.error(
                    "{} Aux Block failed to submit to the server, "
                    "server returned {}!".format(self.config['currency'], res),
                    exc_info=True)
            sleep(1)
        else:
            self.block_stats['rejects'] += 1

        self.block_stats['last_solve_height'] = aux_data['height'] + 1
        self.block_stats['last_solve_worker'] = "{}.{}".format(address, worker)
        self.block_stats['last_solve_time'] = datetime.datetime.utcnow()

    @loop(interval='work_interval')
    def _check_new_jobs(self, reason=None):
        if reason:
            self.logger.info("Updating {} aux work from a signal recieved!"
                             .format(self.config['currency']))

        try:
            auxblock = self.call_rpc('getauxblock')
        except RPCException:
            sleep(2)
            return False

        hash = int(auxblock['hash'], 16)
        if auxblock['hash'] != self.last_work['hash']:
            # We fetch the block height so we can see if the hash changed
            # because of a new network block, or because new transactions
            try:
                height = self.call_rpc('getblockcount')
            except RPCException:
                sleep(2)
                return False

            target_int = pack.IntType(256).unpack(auxblock['target'].decode('hex'))
            self.last_work.update(dict(
                hash=hash,
                target=target_int,
                type=self.config['currency'],
                height=height,
                found_block=self.found_block,
                chainid=auxblock['chainid']
            ))

            # only push the job if there's a new block height discovered.
            if self.current_net['height'] != height:
                self.current_net['height'] = height
                self.jobmanager.generate_job(push=True, flush=self.config['flush'])
                self._incr("work_restarts")
                self._incr("new_jobs")
            else:
                self.new_job.set()
                self.new_job.clear()
                self._incr("new_jobs")

            self.current_net['difficulty'] = bitcoin_data.target_to_difficulty(target_int)
            self.logger.info("New aux work announced! Diff {:,.4f}. Height {:,}"
                             .format(self.current_net['difficulty'], height))

        return True

    @property
    def status(self):
        dct = dict(block_stats=self.block_stats,
                   current_net=self.current_net)
        chop = len(self.prefix)
        dct.update({key[chop:]: self.server[key].summary()
                    for key in self.one_min_stats})
        return dct
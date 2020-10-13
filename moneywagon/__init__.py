from __future__ import print_function
import sys
from binascii import hexlify
from tabulate import tabulate
import hashlib

from base58 import b58decode_check, b58encode_check

from .core import (
    AutoFallbackFetcher, enforce_service_mode, get_optimal_services, get_magic_bytes,
    RevertToPrivateMode, CurrencyNotSupported, NoService, NoServicesDefined, Service
)
from .historical_price import Quandl
from .crypto_data import crypto_data
from bitcoin import sha256, pubtoaddr, privtopub, encode_privkey, encode_pubkey, privkey_to_address
from moneywagon.services import _get_all_services

ALL_SERVICES = _get_all_services()
EXCHANGE_SERVICES = _get_all_services(just_exchange=True)

is_py2 = False
if sys.version_info <= (3,0):
    is_py2 = True

class CompositeResponse(object):
    def __init__(self, service1, service2):
        self.service1 = service1
        self.service2 = service2

    def json(self):
        return {
            self.service1.name: self.service1.last_raw_response.json(),
            self.service2.name: self.service2.last_raw_response.json(),
        }

class CompositeService(object):
    """
    This object mimicks the Service class and is used when the price fetcher has
    to fetch two different price sources. This object is only used when invoking
    `report_services`.
    """
    def __init__(self, services1, services2, via):
        service1 = services1[0]
        service2 = services2[0]

        self.name = "%s -> %s (via %s)" % (
            service1.name, getattr(service2, 'name', 'None'), via.upper()
        )
        self.last_url = "%s, %s" % (service1.last_url, getattr(service2, 'last_url', None))
        self.last_raw_response = CompositeResponse(service1, service2)
        self.service_id = "%d+%d" % (service1.service_id, service2.service_id)

    def __repr__(self):
        return "<Composite Service: %s>" % self.name


def _try_price_fetch(services, args, modes):
    try:
        return enforce_service_mode(
            services, CurrentPrice, args, modes=modes
        )
    except NoService as exc:
        return exc

def get_current_price(crypto, fiat, services=None, convert_to=None, helper_prices=None, **modes):
    """
    High level function for getting current exchange rate for a cryptocurrency.
    If the fiat value is not explicitly defined, it will try the wildcard service.
    if that does not work, it tries converting to an intermediate cryptocurrency
    if available.
    """
    fiat = fiat.lower()
    args = {'crypto': crypto, 'fiat': fiat, 'convert_to': convert_to}

    if not services:
        services = get_optimal_services(crypto, 'current_price')

    if fiat in services:
        # first, try service with explicit fiat support
        try_services = services[fiat]
        result = _try_price_fetch(try_services, args, modes)
        if not isinstance(result, Exception):
            return result

    if '*' in services:
        # then try wildcard service
        try_services = services['*']
        result = _try_price_fetch(try_services, args, modes)
        if not isinstance(result, Exception):
            return result

    def _do_composite_price_fetch(crypto, convert_crypto, fiat, helpers, modes):
        before = modes.get('report_services', False)
        modes['report_services'] = True
        services1, converted_price = get_current_price(crypto, convert_crypto, **modes)
        if not helpers or convert_crypto not in helpers[fiat]:
            services2, fiat_price = get_current_price(convert_crypto, fiat, **modes)
        else:
            services2, fiat_price = helpers[fiat][convert_crypto]

        modes['report_services'] = before

        if modes.get('report_services', False):
            #print("composit service:", crypto, fiat, services1, services2)
            serv = CompositeService(services1, services2, convert_crypto)
            return [serv], converted_price * fiat_price
        else:
            return converted_price * fiat_price

    all_composite_cryptos = ['btc', 'ltc', 'doge', 'uno']

    if fiat in all_composite_cryptos:
        raise result

    composite_exc_msg = ""
    if crypto in all_composite_cryptos: all_composite_cryptos.remove(crypto)
    for composite_attempt in all_composite_cryptos:
        if composite_attempt in services and services[composite_attempt]:
            try:
                result = _do_composite_price_fetch(
                    crypto, composite_attempt, fiat, helper_prices, modes
                )
            except NoService as exc:
                composite_exc_msg += " " + str(exc)
            else:
                return result
    else:
        result = NoService(composite_exc_msg)

    raise result

def get_fiat_exchange_rate(from_fiat, to_fiat):
    from moneywagon.services import FreeCurrencyConverter
    c = FreeCurrencyConverter()
    return c.get_fiat_exchange_rate(from_fiat, to_fiat)

def get_address_balance(crypto, address=None, addresses=None, services=None, **modes):
    if not services:
        services = get_optimal_services(crypto, 'address_balance')

    args = {'crypto': crypto}

    if address:
        args['address'] = address
    elif addresses:
        args['addresses'] = addresses
    else:
        raise Exception("Either address or addresses but not both")

    results = enforce_service_mode(
        services, AddressBalance, args, modes=modes
    )

    if modes.get('private') and addresses:
        results['total_balance'] = sum(results.values())

    if modes.get('private') and modes.get('report_services', False):
        # private mode does not return services (its not practical),
        # an empty list is returned in its place to simplify the API.
        return [], results

    return results

def get_historical_transactions(crypto, address=None, addresses=None, services=None, **modes):
    if not services:
        services = get_optimal_services(crypto, 'historical_transactions')

    kwargs = {'crypto': crypto}
    if addresses:
        kwargs['addresses'] = addresses
    if address:
        kwargs['address'] = address

    try:
        txs = enforce_service_mode(
            services, HistoricalTransactions, kwargs, modes=modes
        )
    except RevertToPrivateMode:
        # no services implement get_historical_transactions_multi...
        modes['private'] = 1
        if modes.get('verbose'):
            print("Can't make with single API call. Retrying with private mode")
        txs = enforce_service_mode(
            services, HistoricalTransactions, kwargs, modes=modes
        )

    if modes.get('private'):
        # private mode returns items indexed by address, this only makes sense to do
        # for address balance, so remove it here
        just_txs = []
        [just_txs.extend(x) for x in txs.values()]
        txs = sorted(just_txs, key=lambda tx: tx['date'], reverse=True)

        no_duplicates = []
        all_txids = []
        for tx in txs:
            # private mode may return duplicate txs, remove them here.
            if tx['txid'] in all_txids:
                continue
            all_txids.append(tx['txid'])
            no_duplicates.append(tx)

        txs = no_duplicates

        if modes.get('report_services', False):
            # private mode does not return services (its not practical),
            # an empty list is returned in its place to simplify the API.
            return [], txs



    return txs

def get_single_transaction(crypto, txid, services=None, **modes):
    if not services:
        services = get_optimal_services(crypto, 'single_transaction')

    return enforce_service_mode(
        services, SingleTransaction, {'crypto': crypto, 'txid': txid}, modes=modes
    )


def get_unspent_outputs(crypto, address=None, addresses=None, services=None, **modes):
    if not services:
        services = get_optimal_services(crypto, 'unspent_outputs')

    kwargs = {'crypto': crypto}
    if addresses:
        kwargs['addresses'] = addresses
    if address:
        kwargs['address'] = address

    try:
        utxos = enforce_service_mode(
            services, UnspentOutputs, kwargs, modes=modes
        )
    except RevertToPrivateMode:
        # no services implement get_unspent_outputs_multi...
        modes['private'] = 1
        if modes.get('verbose'):
            print("Can't make with single API call. Retrying with private mode")
        utxos = enforce_service_mode(
            services, UnspentOutputs, kwargs, modes=modes
        )

    if modes.get('private'):
        # private mode returns items indexed by address, this only makes sense to do
        # for address balance, so remove it here
        just_utxos = []
        [just_utxos.extend(x) for x in utxos.values()]
        utxos = sorted(just_utxos, key=lambda tx: tx['output'])
        if modes.get('report_services', False):
            # private mode does not return services (its not practical),
            # an empty list is returned in its place to satisfy the API.
            return [], utxos

    return utxos


def get_historical_price(crypto, fiat, date):
    """
    Only one service is defined for geting historical price, so no fetching modes
    are needed.
    """
    return HistoricalPrice().action(crypto, fiat, date)


def push_tx(crypto, tx_hex, services=None, **modes):
    if not services:
        services = get_optimal_services(crypto, 'push_tx')
    return enforce_service_mode(
        services, PushTx, {'crypto': crypto, 'tx_hex': tx_hex}, modes=modes
    )


def get_block(crypto, block_number=None, block_hash=None, latest=False, services=None, **modes):
    if not services:
        services = get_optimal_services(crypto, 'get_block')
    kwargs = dict(crypto=crypto, block_number=block_number, block_hash=block_hash, latest=latest)
    return enforce_service_mode(
        services, GetBlock, kwargs, modes=modes
    )


def get_optimal_fee(crypto, tx_bytes, **modes):
    """
    Get the optimal fee based on how big the transaction is. Currently this
    is only provided for BTC. Other currencies will return $0.02 in satoshi.
    """
    try:
        services = get_optimal_services(crypto, 'get_optimal_fee')
    except NoServicesDefined:
        convert = get_current_price(crypto, 'usd')
        fee = int(0.02 / convert * 1e8)

        if modes.get('report_services'):
            return [None], fee
        else:
            return fee

    fee = enforce_service_mode(
        services, OptimalFee, dict(crypto=crypto, tx_bytes=tx_bytes), modes=modes
    )
    if modes.get('report_services'):
        return fee[0], int(fee[1])
    else:
        return int(fee)



def get_onchain_exchange_rates(deposit_crypto=None, withdraw_crypto=None, **modes):
    """
    Gets exchange rates for all defined on-chain exchange services.
    """
    from moneywagon.onchain_exchange import ALL_SERVICES

    rates = []
    for Service in ALL_SERVICES:
        srv = Service(verbose=modes.get('verbose', False))
        rates.extend(srv.onchain_exchange_rates())

    if deposit_crypto:
        rates = [x for x in rates if x['deposit_currency']['code'] == deposit_crypto.upper()]

    if withdraw_crypto:
        rates = [x for x in rates if x['withdraw_currency']['code'] == withdraw_crypto.upper()]

    if modes.get('best', False):
        return max(rates, key=lambda x: float(x['rate']))

    return rates


def generate_keypair(crypto, seed, password=None):
    """
    Generate a private key and publickey for any currency, given a seed.
    That seed can be random, or a brainwallet phrase.
    """
    if crypto in ['eth', 'etc']:
        raise CurrencyNotSupported("Ethereums not yet supported")

    pub_byte, priv_byte = get_magic_bytes(crypto)
    priv = sha256(seed)
    pub = privtopub(priv)

    priv_wif = encode_privkey(priv, 'wif_compressed', vbyte=priv_byte)
    if password:
        # pycrypto etc. must be installed or this will raise ImportError, hence inline import.
        from .bip38 import Bip38EncryptedPrivateKey
        priv_wif = str(Bip38EncryptedPrivateKey.encrypt(crypto, priv_wif, password))

    compressed_pub = encode_pubkey(pub, 'hex_compressed')
    ret = {
        'public': {
            'hex_uncompressed': pub,
            'hex': compressed_pub,
            'address': pubtoaddr(compressed_pub, pub_byte)
        },
        'private': {
            'wif': priv_wif
        }
    }
    if not password:
        # only these are valid when no bip38 password is supplied
        ret['private']['hex'] = encode_privkey(priv, 'hex_compressed', vbyte=priv_byte)
        ret['private']['hex_uncompressed'] = encode_privkey(priv, 'hex', vbyte=priv_byte)
        ret['private']['wif_uncompressed'] = encode_privkey(priv, 'wif', vbyte=priv_byte)

    return ret

def wif_to_address(crypto, wif):
    try:
        return privkey_to_address(wif, crypto_data[crypto]['address_version_byte'])
    except KeyError:
        raise CurrencyNotSupported("Currency not yet supported")

def sweep(crypto, private_key, to_address, fee=None, password=None, **modes):
    """
    Move all funds by private key to another address.
    """
    from moneywagon.tx import Transaction
    tx = Transaction(crypto, verbose=modes.get('verbose', False))
    tx.add_inputs(private_key=private_key, password=password, **modes)
    tx.change_address = to_address
    tx.fee(fee)

    return tx.push()


def get_explorer_url(crypto, address=None, txid=None, blocknum=None, blockhash=None):
    services = crypto_data[crypto]['services']['address_balance']
    urls = []
    context = {'crypto': crypto}
    if address:
        attr = "explorer_address_url"
        context['address'] = address
    elif txid:
        attr = "explorer_tx_url"
        context['txid'] = txid
    elif blocknum:
        attr = "explorer_blocknum_url"
        context['blocknum'] = blocknum
    elif blockhash:
        attr = "explorer_blockhash_url"
        context['blockhash'] = blockhash

    for service in services:
        template = getattr(service, attr)
        context['domain'] = service.domain
        context['protocol'] = service.protocol

        if hasattr(service, '_get_coin'):
            # used for when a service uses another name for a certain coin
            # other than the standard three letter currency code.
            context['coin'] = service._get_coin(crypto)

        if template:
            # render the explorer url temlate
            urls.append(template.format(**context))

    return urls


def guess_currency_from_address(address):
    """
    Given a crypto address, find which currency it likely belongs to.
    Raises an exception if it can't find a match. Raises exception if address
    is invalid.
    """
    if is_py2:
        fixer = lambda x: int(x.encode('hex'), 16)
    else:
        fixer = lambda x: x # does nothing

    first_byte = fixer(b58decode_check(address)[0])
    double_first_byte = fixer(b58decode_check(address)[:2])

    hits = []
    for currency, data in crypto_data.items():
        if hasattr(data, 'get'): # skip incomplete data listings
            version = data.get('address_version_byte', None)
            if version is not None and version in [double_first_byte, first_byte]:
                hits.append([currency, data['name']])

    if hits:
        return hits

    raise ValueError("Unknown Currency with first byte: %s" % first_byte)

def change_version_byte(address, new_version=None, new_crypto=None):
    """
    Convert the passed in address (or any base58 encoded string), and change the
    version byte to `new_version`.
    """
    if not new_version and new_crypto:
        try:
            new_version = crypto_data[new_crypto]['address_version_byte']
        except KeyError:
            raise CurrencyNotSupported("Unknown currency symbol: " + new_crypto)

        if not new_version:
            raise CurrencyNotSupported("Can't yet make %s addresses." % new_crypto)

    payload = b58decode_check(address)[1:]
    if is_py2:
        byte = chr(new_version)
    else:
        byte = bytes(chr(new_version), 'ascii')

    return b58encode_check(byte + payload)

class OptimalFee(AutoFallbackFetcher):
    def action(self, crypto, tx_bytes):
        crypto = crypto.lower()
        return self._try_services("get_optimal_fee", crypto, tx_bytes)

    def no_service_msg(self, crypto, tx_bytes):
        return "Could not get optimal fee for: %s" % crypto


class SingleTransaction(AutoFallbackFetcher):
    def action(self, crypto, txid):
        crypto = crypto.lower()
        return self._try_services("get_single_transaction", crypto, txid)

    @classmethod
    def strip_for_consensus(cls, result):
        return "%.8f %.8f" % (result['total_in'], result['total_out'])

    def no_service_msg(self, crypto, txid=None, txids=None):
        return "Could not get transaction info for: %s:%s" % (crypto, txid or ', '.join(txids))


class GetBlock(AutoFallbackFetcher):
    def action(self, crypto, block_number='', block_hash='', latest=False):
        if sum([type(block_number)==int, bool(block_hash), bool(latest)]) != 1:
            raise ValueError("Only one of `block_hash`, `latest`, or `block_number` allowed.")
        return self._try_services(
            'get_block', crypto, block_number=block_number, block_hash=block_hash, latest=latest
        )

    def no_service_msg(self, crypto, block_number=None, block_hash=None, latest=False):
        block = block_number or block_hash or ('latest' if latest else 'None')
        return "Could not get %s block: %s" % (
            crypto, block
        )

    @classmethod
    def strip_for_consensus(self, result):
        return "%s, %s, %s" % (
            result['hash'], result['block_number'], result['size']
        )


class HistoricalTransactions(AutoFallbackFetcher):
    def action(self, crypto, address=None, addresses=None):
        if addresses:
            method_name = "get_transactions_multi"
            kwargs = dict(addresses=addresses)

        if address:
            method_name = "get_transactions"
            kwargs = dict(address=address)

        txs = self._try_services(method_name, crypto, **kwargs)
        return sorted(txs, key=lambda tx: tx['date'], reverse=True)

    def no_service_msg(self, crypto, address=None, addresses=None):
        return "Could not get transactions for: %s:%s" % (crypto, address or ', '.join(addresses))

    @classmethod
    def strip_for_consensus(cls, results):
        stripped = []
        for result in results:
            result.sort(key=lambda x: x['date'])
            stripped.append(
                ", ".join(
                    ["[id: %s, amount: %s]" % (x['txid'], x['amount']) for x in result]
                )
            )
        return stripped


class UnspentOutputs(AutoFallbackFetcher):
    def action(self, crypto, address=None, addresses=None):
        if addresses:
            method_name = "get_unspent_outputs_multi"
            kwargs = dict(addresses=addresses)

        if address:
            method_name = "get_unspent_outputs"
            kwargs = dict(address=address)

        utxos = self._try_services(method_name, crypto=crypto, **kwargs)
        return sorted(utxos, key=lambda x: x['output'])

    def no_service_msg(self, crypto, address=None, addresses=None):
        return "Could not get unspent outputs for: %s:%s" % (crypto, address or ', '.join(addresses))

    @classmethod
    def strip_for_consensus(cls, results):
        stripped = []
        for result in results:
            result.sort(key=lambda x: x['output'])
            stripped.append(
                ", ".join(
                    ["[output: %s, value: %s]" % (x['output'], x['amount']) for x in result]
                )
            )
        return stripped


class CurrentPrice(AutoFallbackFetcher):
    def action(self, crypto, fiat, convert_to=None):
        if crypto.lower() == fiat.lower():
            return (1.0, 'math')

        ret = self._try_services('get_current_price', crypto=crypto, fiat=fiat)
        if convert_to:
            return ret / get_fiat_exchange_rate(from_fiat=fiat, to_fiat=convert_to)
        return ret


    def simplify_for_average(self, value):
        return value

    def no_service_msg(self, crypto, fiat):
        return "Can not find current price for %s->%s" % (crypto, fiat)


class AddressBalance(AutoFallbackFetcher):
    def action(self, crypto, address=None, addresses=None, confirmations=1):
        kwargs = dict(crypto=crypto, confirmations=confirmations)

        if address:
            method_name = "get_balance"
            kwargs['address'] = address

        if addresses:
            method_name = "get_balance_multi"
            kwargs['addresses'] = addresses

        results = self._try_services(method_name, **kwargs)

        if addresses and 'total_balance' not in results:
            results['total_balance'] = sum(results.values())

        return results

    def no_service_msg(self, crypto, address=None, addresses=None, confirmations=1):
        return "Could not get confirmed address balance for: %s" % crypto


class PushTx(AutoFallbackFetcher):
    def action(self, crypto, tx_hex):
        return self._try_services("push_tx", crypto=crypto, tx_hex=tx_hex)

    def no_service_msg(self, crypto, tx_hex):
        return "Could not push this %s transaction." % crypto


class HistoricalPrice(object):
    """
    This one doesn't inherit from AutoFallbackFetcher because there is only one
    historical price API service at the moment.
    """
    def __init__(self, responses=None, verbose=False):
        self.service = Quandl(responses, verbose=verbose)

    def action(self, crypto, fiat, at_time):
        crypto = crypto.lower()
        fiat = fiat.lower()

        if crypto != 'btc' and fiat != 'btc':
            # two external requests and some math is going to be needed.
            from_btc, source1, date1 = self.service.get_historical(crypto, 'btc', at_time)
            to_altcoin, source2, date2 = self.service.get_historical('btc', fiat, at_time)
            return (from_btc * to_altcoin), "%s x %s" % (source1, source2), date1
        else:
            return self.service.get_historical(crypto, fiat, at_time)

    @property
    def responses(self):
        return self.service.responses


def service_table(format='simple', authenticated=False):
    """
    Returns a string depicting all services currently installed.
    """
    if authenticated:
        all_services = ExchangeUniverse.get_authenticated_services()
    else:
        all_services = ALL_SERVICES

    if format == 'html':
        linkify = lambda x: "<a href='{0}' target='_blank'>{0}</a>".format(x)
    else:
        linkify = lambda x: x

    ret = []
    for service in sorted(all_services, key=lambda x: x.service_id):
        ret.append([
            service.service_id,
            service.__name__, linkify(service.api_homepage.format(
                domain=service.domain, protocol=service.protocol
            )),
            ", ".join(service.supported_cryptos or [])
        ])

    return tabulate(ret, headers=['ID', 'Name', 'URL', 'Supported Currencies'], tablefmt=format)

def wif_to_hex(wif):
    """
    Convert a WIF encded private key and return the raw hex encoded private key
    This function works for all bitcoin-API compatable coins.
    """
    return hexlify(b58decode_check(wif)[1:]).upper()

class ExchangeUniverse(object):
    def __init__(self, verbose=False, services=None, timeout=6):
        self._all_pairs = {}
        self.services = [
            x(verbose=verbose, timeout=timeout) if not isinstance(x, Service) else x
            for x in (services or ALL_SERVICES)
        ]
        self.verbose = verbose

    @classmethod
    def get_authenticated_services(self):
        return [x for x in ALL_SERVICES if x().api_key]

    def get_benchmarks(self):
        ret = {}
        for s in self.services:
            if not s.total_external_fetch_duration:
                continue # no call was made to this service, do not include in benchmark
            ret[s.name] = s.total_external_fetch_duration.total_seconds()
        return ret

    def fetch_pairs(self):
        if self._all_pairs:
            return

        for service in self.services:
            try:
                self._all_pairs[service.name] = service.get_pairs()
            except NotImplementedError:
                pass
            except Exception as exc:
                print("%s returned error: %s" % (service.__class__.__name__, exc))

    def find_pair(self, crypto="", fiat="", verbose=False):
        """
        This utility is used to find an exchange that supports a given exchange pair.
        """
        self.fetch_pairs()
        if not crypto and not fiat:
            raise Exception("Fiat or Crypto required")

        def is_matched(crypto, fiat, pair):
            if crypto and not fiat:
                return pair.startswith("%s-" % crypto)
            if crypto and fiat:
                return pair == "%s-%s" % (crypto, fiat)
            if not crypto:
                return pair.endswith("-%s" % fiat)

        matched_pairs = {}
        for Service, pairs in self._all_pairs.items():
            matched = [p for p in pairs if is_matched(crypto, fiat, p)]
            if matched:
                matched_pairs[Service] = matched

        return matched_pairs

    def all_cryptos(self):
        self.fetch_pairs()
        all_cryptos = set()
        for Service, pairs in self._all_pairs.items():
            for pair in pairs:
                crypto = pair.split("-")[0]
                all_cryptos.add(crypto)
        return sorted(all_cryptos)

    def most_supported(self, skip_supported=False):
        self.fetch_pairs()
        counts = []
        for crypto in self.all_cryptos():
            if skip_supported and crypto in crypto_data:
                continue
            matched = self.find_pair(crypto=crypto)
            count = sum(len(x) for x in matched.values())
            counts.append([crypto, count])

        return sorted(counts, key=lambda x: x[1], reverse=True)

def wif_to_address(crypto, wif):
    if is_py2:
        wif_byte = int(hexlify(b58decode_check(wif)[0]), 16)
    else:
        wif_byte = b58decode_check(wif)[0]

    if not wif_byte == crypto_data[crypto.lower()]['private_key_prefix']:
        msg = 'WIF encoded with wrong prefix byte. Are you sure this is a %s address?' % crypto.upper()
        raise Exception(msg)

    address_byte = crypto_data[crypto.lower()]['address_version_byte']
    return privkey_to_address(wif, address_byte)

def watch_mempool(crypto, callback=lambda tx: print(tx['txid']), verbose=False):
    from socketIO_client import SocketIO
    if verbose:
        import logging
        logging.getLogger('socketIO-client').setLevel(logging.DEBUG)
        logging.basicConfig()

    services = get_optimal_services(crypto, 'address_balance')

    for service in services:
        socketio_url = service.socketio_url
        if not socketio_url:
            continue
        try:
            socketIO = SocketIO(socketio_url, verify=False,) # namespace=LoggingNamespace)
            socketIO.emit('subscribe', 'inv');
            socketIO.on('tx', callback)
            socketIO.wait()
        except KeyboardInterrupt:
            return
        except:
            print("failed")

from BTrees import OOBTree
from datetime import datetime, date, timedelta
from persistent import Persistent
from .vulnerability import Vulnerability
import gzip
import json
import logging
import os
import os.path as p
import requests
import transaction
import ZODB
import ZODB.FileStorage

DEFAULT_MIRROR = 'https://nvd.nist.gov/feeds/json/cve/1.1/'
DEFAULT_CACHE_DIR = '~/.cache/vulnix'

logger = logging.getLogger(__name__)


class NVD(object):
    """Access to the National Vulnerability Database.

    https://nvd.nist.gov/
    """

    def __init__(self, mirror=DEFAULT_MIRROR, cache_dir=DEFAULT_CACHE_DIR):
        self.mirror = mirror.rstrip('/') + '/'
        self.cache_dir = p.expanduser(cache_dir)
        current = date.today().year
        self.available_archives = [y for y in range(current-5, current+1)]

    def __enter__(self):
        """Keeps database connection open while in this context."""
        logger.debug('Using cache in %s', self.cache_dir)
        os.makedirs(self.cache_dir, exist_ok=True)
        storage = ZODB.FileStorage.FileStorage(
            p.join(self.cache_dir, 'Data.fs'))
        self._db = ZODB.DB(storage)
        self._connection = self._db.open()
        self._root = self._connection.root()
        self._root.setdefault('advisory', OOBTree.OOBTree())
        self._root.setdefault('by_product', OOBTree.OOBTree())
        self._root.setdefault('meta', Meta())
        try:
            del self._root['archives']
        except KeyError:
            pass
        return self

    def __exit__(self, exc_type=None, exc_value=None, exc_tb=None):
        if exc_type is None:
            if self._root['meta'].should_pack():
                logger.debug('Packing database')
                transaction.commit()
                self._db.pack()
            transaction.commit()
        else:
            transaction.abort()
        self._connection.close()
        self._connection = None
        self._db = None

    def relevant_archives(self):
        """Returns list of NVD archives to check.

        If there was an update within the last hour, noting is done. If
        the last update was recent enough to be covered by the
        'modified' feed, only that is checked. Else, all feeds are
        checked.
        """
        last_update = self._root['meta'].last_update
        if last_update > datetime.now() - timedelta(hours=1):
            return []
        # the "modified" feed is sufficient if used frequently enough
        if last_update > datetime.now() - timedelta(days=7):
            return ['modified']
        return self.available_archives + ['modified']

    def update(self):
        """Download archives (if changed) and add CVEs to database."""
        for a in self.relevant_archives():
            arch = Archive(a)
            arch.download(self.mirror, self._root['meta'])
            self.add(arch)
        self.reindex()

    def add(self, archive):
        advisories = self._root['advisory']
        for (cve_id, adv) in archive.items():
            advisories[cve_id] = adv

    def reindex(self):
        """Regenerate product index."""
        del self._root['by_product']
        bp = OOBTree.OOBTree()
        for vuln in self._root['advisory'].values():
            for prod in (n.product for n in vuln.nodes):
                if prod not in bp:
                    bp[prod] = []
                bp[prod].append(vuln)
        self._root['by_product'] = bp

    def by_id(self, cve_id):
        """Returns vuln or raises KeyError."""
        return self._root['advisory'][cve_id]

    def by_product(self, product):
        """Returns list of matching vulns or empty list."""
        try:
            return self._root['by_product'][product]
        except KeyError:
            return []

    def affected(self, pname, version):
        """Returns list of matching vulnerabilities."""
        res = set()
        for vuln in self.by_product(pname):
            if vuln.match(pname, version):
                res.add(vuln)
        return res


class Archive:

    """Single JSON data structure from NIST NVD."""

    def __init__(self, name):
        """Creates JSON feed object.

        `name` consists of a year or "modified".
        """
        self.name = name
        self.download_uri = 'nvdcve-1.1-{}.json.gz'.format(name)
        self.advisories = {}

    def download(self, mirror, metadata):
        """Fetches compressed JSON data from NIST.

        Nothing is done if we have already seen the same version of
        the feed before.
        """
        url = mirror + self.download_uri
        logger.info('Loading %s', url)
        r = requests.get(url, headers=metadata.headers_for(url))
        r.raise_for_status()
        if r.status_code == 200:
            logger.debug('Parsing JSON feed "%s"', self.name)
            self.parse(gzip.decompress(r.content))
            metadata.update_headers_for(url, r.headers)
            metadata.last_update = datetime.now()
        else:
            logger.debug('Skipping JSON feed "%s" (%s)', self.name, r.reason)

    def parse(self, nvd_json):
        raw = json.loads(nvd_json)
        for item in raw['CVE_Items']:
            try:
                vuln = Vulnerability.parse(item)
                self.advisories[vuln.cve_id] = vuln
            except ValueError:
                logger.debug('Failed to parse NVD item: %s', item)

    def items(self):
        return self.advisories.items()


class Meta(Persistent):
    """Metadate for database maintenance control"""

    pack_counter = 0
    last_update = datetime(1970, 1, 1)
    etag = None

    def should_pack(self):
        self.pack_counter += 1
        if self.pack_counter > 25:
            self.pack_counter = 0
            return True
        return False

    def headers_for(self, url):
        """Returns dict of additional request headers."""
        if self.etag and url in self.etag:
            return {'If-None-Match': self.etag[url]}
        return {}

    def update_headers_for(self, url, resp_headers):
        """Updates self from HTTP response headers."""
        if 'ETag' in resp_headers:
            if self.etag is None:
                self.etag = OOBTree.OOBTree()
            self.etag[url] = resp_headers['ETag']

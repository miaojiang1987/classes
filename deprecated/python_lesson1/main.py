from __future__ import print_function

import sqlite3
import os.path
import os
import urllib
import sys
import zipfile
import hashlib
import itertools
import attr
import types
import random
import shutil
import tempfile
import subprocess
import multiprocessing
import functools


# NIST Special Database 4
# http://www.nist.gov/srd/nistsd4.cfm
DATASET_URL = 'https://s3.amazonaws.com/nist-srd/SD4/NISTSpecialDatabase4GrayScaleImagesofFIGS.zip'
DATASET_SHA256 = '4db6a8f3f9dc14c504180cbf67cdf35167a109280f121c901be37a80ac13c449'


# NIST Biometric Image Software
# http://www.nist.gov/itl/iad/ig/nbis.cfm
NBIS_URL = 'http://nigos.nist.gov:8080/nist/nbis/nbis_v5_0_0.zip'


#### utilities


def take(n, iterable):
    return itertools.islice(iterable, n )


def zipWith(function, *iterables):
    for group in itertools.izip(*iterables):
        yield function(*group)


def uncurry(function):
    @functools.wraps(function)
    def wrapper(args):
        return function(*args)
    return wrapper


def fetch_url(url, sha256, prefix='.', checksum_blocksize=2**20, dryRun=False):
    if not os.path.exists(prefix):
        os.makedirs(prefix)

    local = os.path.join(prefix, os.path.basename(url))

    if dryRun: return local

    if os.path.exists(local):
        print ('Verifying checksum')
        chk = hashlib.sha256()
        with open(local, 'rb') as fd:
            while True:
                bits = fd.read(checksum_blocksize)
                if not bits: break
                chk.update(bits)
        if sha256 == chk.hexdigest():
            return local

    print ('Downloading', url)

    def report(sofar, blocksize, totalsize):
        msg = '{}%\r'.format(100 * sofar * blocksize / totalsize, 100)
        sys.stderr.write(msg)

    urllib.urlretrieve(url, local, report)

    return local


def prepare_dataset(url=None, sha256=None, prefix='.', skip=False):
    url = url or DATASET_URL
    sha256 = sha256 or DATASET_SHA256
    local = fetch_url(url, sha256=sha256, prefix=prefix, dryRun=skip)

    if not skip:
        print ('Extracting', local, 'to', prefix)
        with zipfile.ZipFile(local, 'r') as zip:
            zip.extractall(prefix)

    name, _ = os.path.splitext(local)
    return name
    

def locate_paths(path_md5list, prefix):
    with open(path_md5list) as fd:
        for line in itertools.imap(str.strip, fd):
            parts = line.split()
            if not len(parts) == 2: continue
            md5sum, path = parts
            chksum = Checksum(value=md5sum, kind='md5')
            filepath = os.path.join(prefix, path)
            yield Path(checksum=chksum, filepath=filepath)


def locate_images(paths):

    def predicate(path):
        _, ext = os.path.splitext(path.filepath)
        return ext in ['.png']

    for path in itertools.ifilter(predicate, paths):
        yield image(id=path.checksum.value, path=path)


@attr.s(slots=True)
class Checksum(object):
    value = attr.ib()
    kind = attr.ib(validator=lambda o, a, v: v in 'md5 sha1 sha224 sha256 sha384 sha512'.split())

@attr.s(slots=True)
class Path(object):
    checksum = attr.ib()
    filepath = attr.ib()


@attr.s(slots=True)
class image(object):
    id = attr.ib()
    path = attr.ib()


@attr.s(slots=True)
class mindtct(object):
    image = attr.ib()
    xyt = attr.ib()


def mindtct_from_image(image):
    imgpath = os.path.abspath(image.path.filepath)
    tempdir = tempfile.mkdtemp()
    oroot = os.path.join(tempdir, 'result')

    cmd = ['mindtct', imgpath, oroot]

    try:
        subprocess.check_call(cmd)

        with open(oroot + '.xyt') as fd:
            xyt = fd.read()

        result = mindtct(image=image.id, xyt=xyt)
        return result

    finally:
        shutil.rmtree(tempdir)


@attr.s(slots=True)
class bozorth3(object):
    probe = attr.ib()
    gallery = attr.ib()
    score = attr.ib()


    @staticmethod
    def sql_stmt_create_table():
        return 'CREATE TABLE IF NOT EXISTS bozorth3 (probe TEXT, gallery TEXT, score NUMERIC)'


    @staticmethod
    def sql_prepared_stmt_insert():
        return 'INSERT INTO bozorth3 VALUES (?, ?, ?)'


    def sql_insert_values(self):
        return self.probe, self.gallery, self.score


@attr.s(slots=True)
class bozorth3_input(object):
    probe = attr.ib()
    gallery = attr.ib()

    def run(self):
        if isinstance(self.gallery, mindtct):
            return bozorth3_from_group(self.probe, self.gallery)
        elif isinstance(self.gallery, types.ListType):
            return bozorth3_from_one_to_many(self.probe, self.gallery)
        else:
            raise ValueError('Unhandled type for gallery: {}'.format(type(gallery)))


def run_bozorth3(input):
    return input.run()


def bozorth3_from_group(probe, gallery):
    tempdir = tempfile.mkdtemp()
    probeFile = os.path.join(tempdir, 'probe.xyt')
    galleryFile = os.path.join(tempdir, 'gallery.xyt')

    with open(probeFile, 'wb')   as fd: fd.write(probe.xyt)
    with open(galleryFile, 'wb') as fd: fd.write(gallery.xyt)

    cmd = ['bozorth3', probeFile, galleryFile]

    try:
        result = subprocess.check_output(cmd)
        score = int(result.strip())

        return bozorth3(probe=probe.image, gallery=gallery.image, score=score)
    finally:
        shutil.rmtree(tempdir)


def bozorth3_from_one_to_many(probe, galleryset):
    tempdir = tempfile.mkdtemp()
    probeFile = os.path.join(tempdir, 'probe.xyt')
    galleryFiles = [os.path.join(tempdir, 'gallery%d.xyt' % i) for i, _ in enumerate(galleryset)]

    with open(probeFile, 'wb') as fd: fd.write(probe.xyt)
    for galleryFile, gallery in itertools.izip(galleryFiles, galleryset):
        with open(galleryFile, 'wb') as fd: fd.write(gallery.xyt)

    cmd = ['bozorth3', '-p', probeFile] + galleryFiles

    try:
        result = subprocess.check_output(cmd).strip()
        scores = map(int, result.split('\n'))
        return [bozorth3(probe=probe.image, gallery=gallery.image, score=score)
                for score, gallery in zip(scores, galleryset)]
    finally:
        shutil.rmtree(tempdir)


if __name__ == '__main__':

    prefix = sys.argv[1]
    md5listpath = sys.argv[2]
    perc_probe = float(sys.argv[3])
    perc_gallery = float(sys.argv[4])

    pool = multiprocessing.Pool()
    conn = sqlite3.connect('scores.db')
    cursor = conn.cursor()

    cursor.execute(bozorth3.sql_stmt_create_table())


    dataprefix = prepare_dataset(prefix=prefix, skip=True)

    print ('Loading images')
    paths = locate_paths(md5listpath, dataprefix)
    images = locate_images(paths)
    mindtcts = pool.map(mindtct_from_image, images)
    mindtcts = list(mindtcts)


    print ('Generating samples')
    probes  = random.sample(mindtcts, int(perc_probe   * len(mindtcts)))
    gallery = random.sample(mindtcts, int(perc_gallery * len(mindtcts)))
    input   = [bozorth3_input(probe=probe, gallery=gallery) for probe in probes]

    print ('Matching')
    bozorth3s = pool.map(run_bozorth3, input)
    for group in bozorth3s:
        vals = map(bozorth3.sql_insert_values, group)
        cursor.executemany(bozorth3.sql_prepared_stmt_insert(), vals)
        conn.commit()
        map(print, group)


    conn.close()

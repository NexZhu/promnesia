#!/usr/bin/python3
__package__ = 'promnesia'  # ugh. hacky way to make hug work properly...

import argparse
import os
import sys
from datetime import timedelta, datetime
from pathlib import Path
import logging
from functools import lru_cache
from typing import Collection, List, NamedTuple, Dict


from cachew import NTBinder

import pytz

import hug # type: ignore
import hug.types as T # type: ignore

from sqlalchemy import create_engine, MetaData, exists, literal, between, or_, and_ # type: ignore
from sqlalchemy import Column, Table, func # type: ignore


from .common import PathWithMtime, DbVisit, Url, Loc, setup_logger
from . import config as cfg
from .normalise import normalise_url

_ENV_CONFIG = 'PROMNESIA_CONFIG'


# TODO not sure about utc in database... keep orig timezone?

# meh. need this since I don't have hooks in hug to initialize logging properly..
@lru_cache(1)
def get_logger():
    logger = logging.getLogger('promnesia')
    setup_logger(logger, level=logging.DEBUG)
    return logger


@lru_cache(1)
def _get_config(mpath: PathWithMtime) -> cfg.Config:
    get_logger().info('Reloading config: %s', mpath)
    cfg.load_from(mpath.path) # TODO err, not sure if should really bother with hot reloading; it would assert?
    return cfg.get()


def get_config() -> cfg.Config:
    cp = os.environ.get(_ENV_CONFIG)
    assert cp is not None
    return _get_config(PathWithMtime.make(Path(cp)))

# TODO use that?? https://github.com/timothycrosley/hug/blob/develop/tests/test_async.py

# TODO how to return exception in error?

def as_json(v: DbVisit) -> Dict:
    # TODO check utc
    # TODO also local should be suppressed if any other src with this timestamp is present
    dts = v.dt.strftime('%d %b %Y %H:%M')
    loc = v.locator
    # # TODO is locator always present??
    return {
        # TODO do not display year if it's current year??
        'dt': dts,
        'src': v.src,
        'context': v.context,
        'duration': v.duration,
        'locator': {
            'title': loc.title,
            'href' : loc.href,
        },
        'original_url'  : v.orig_url,
        'normalised_url': v.norm_url,
    }


def get_db_path() -> Path:
    config = get_config()
    db_path = Path(config.OUTPUT_DIR) / 'promnesia.sqlite'
    assert db_path.exists()
    return db_path


# TODO maybe, keep db connection? need to recycle it properly..
@lru_cache(1)
# PathWithMtime aids lru_cache in reloading the sqlalchemy binder
def _get_stuff(db_path: PathWithMtime):
    get_logger().info('Reloading DB: %s', db_path)
    # TODO how to open read only?
    engine = create_engine(f'sqlite:///{db_path.path}') # , echo=True)

    binder = NTBinder.make(DbVisit)

    meta = MetaData(engine)
    table = Table('visits', meta, *binder.columns)

    return engine, binder, table


def get_stuff(db_path=None): # TODO better name
    # ok, it will always load from the same db file; but intermediate would be kinda an optional dump.
    if db_path is None:
        db_path = get_db_path()
    return _get_stuff(PathWithMtime.make(db_path))



def search_common(url: str, where):
    logger = get_logger()
    config = get_config()

    logger.info('url: %s', url)
    original_url = url
    url = normalise_url(url)
    logger.info('normalised url: %s', url)

    engine, binder, table = get_stuff()

    query = table.select().where(where(table=table, url=url))

    logger.info('query: %s', query)

    with engine.connect() as conn:
        visits = [binder.from_row(row) for row in conn.execute(query)]

    logger.debug('got %d visits from db', len(visits))

    vlist = []
    for vis in visits:
        dt = vis.dt
        if dt.tzinfo is None:
            # TODO hmm. I guess server and indexer should better agree on timezone...
            # TODO use lazy property in config for indexers?
            ftz = config.FALLBACK_TIMEZONE
            tz = pytz.timezone(ftz) if isinstance(ftz, str) else ftz
            dt = tz.localize(dt)
            vis = vis._replace(dt=dt)
        vlist.append(vis)

    logger.debug('responding with %d visits', len(vlist))
    # TODO respond with normalised result, then frontent could choose how to present children/siblings/whatever?
    return {
        'orginal_url'   : original_url,
        'normalised_url': url,
        'visits': list(map(as_json, vlist)),
    }


@hug.local()
@hug.post('/status')
def status():
    db_path = get_db_path()
    # TODO query count of items in db?
    return {
        # TODO hug stats?
        'status': 'OK',
        'db'    : str(db_path),
    }


@hug.local()
@hug.post('/visits')
def visits(
        url: T.text,
):
    return search_common(
        url=url,
        # odd, doesn't work just with: x or (y and z)
        where=lambda table, url: or_(table.c.norm_url == url, and_(table.c.context != None, table.c.norm_url.like(url + '%'))),
    )


@hug.local()
@hug.post('/search')
def search(
        url: T.text
):
    # TODO rely on hug logger for query
    return search_common(
        url=url,
        where=lambda table, url: table.c.norm_url.like('%' + url + '%'), # TODO FIXME what if url contains %? (and it will!)
    )


@hug.local()
@hug.post('/search_around')
def search_around(
        timestamp: T.number,
):
    delta_back  = timedelta(hours=3).total_seconds()
    delta_front = timedelta(minutes=5).total_seconds()
    # TODO not sure about front.. but it also serves as quick hack to accomodate for all the truncations etc
    return search_common(
        url='http://dummy.org', # TODO remove it from search_common
        # TODO no abs?
        where=lambda table, url: between(
            func.strftime('%s', func.datetime(table.c.dt)) - literal(timestamp),
            literal(-delta_back),
            literal(delta_front),
        ),
    )

@hug.local()
@hug.post('/visited')
def visited(
        urls, # TODO type
):
    logger = get_logger()

    logger.debug(urls)
    norms = [(u, normalise_url(u)) for u in urls]
    # logger.debug('\n'.join(f'{u} -> {nu}' for u, nu in norms))

    nurls = [n[1] for n in norms]
    engine, binder, table = get_stuff()

    snurls = list(sorted(set(nurls)))
    # sqlalchemy doesn't seem to support SELECT FROM (VALUES (...)) in its api
    # also doesn't support array binding...
    # https://stackoverflow.com/questions/13190392/how-can-i-bind-a-list-to-a-parameter-in-a-custom-query-in-sqlalchemy
    bstring = ','.join(f'(:b{i})'   for i, _ in enumerate(snurls))
    bdict = {            f'b{i}': v for i, v in enumerate(snurls)}

    query = f"""
WITH cte(queried) AS (SELECT * FROM (values {bstring}))
SELECT queried
    FROM cte JOIN visits
    ON queried = visits.norm_url
    """
    # hmm that was quite slow...
    # SELECT queried FROM cte WHERE EXISTS (SELECT 1 FROM visits WHERE queried = visits.norm_url)
    logger.debug(bdict)
    logger.debug(query)
    with engine.connect() as conn:
        res = list(conn.execute(query, bdict))
        present = {x[0] for x in res}
    results = [nu in present for nu in nurls]

    # logger.debug('\n'.join(
    #     f'{"X" if v else "-"} {u} -> {nu}' for v, (u, nu) in zip(results, norms)
    # ))
    return results


def run(port: str, config: Path, quiet: bool):
    logger = get_logger()
    env = os.environ.copy()
    # # not sure if there is a simpler way to communicate with the server...
    env[_ENV_CONFIG] = str(config)
    args = [
        'promnesia-server',
        *(['--silent'] if quiet else []),
        '-p', port,
        '-f', __file__,
    ]
    logger.info('Running server: %s', args)
    os.execvpe('hug', args, env)


_DEFAULT_CONFIG = Path('config.py')


def setup_parser(p):
    p.add_argument('--port', type=str, default='13131', help='Port for communicating with extension')
    p.add_argument('--config', type=Path, default=_DEFAULT_CONFIG, help='Path to config')
    p.add_argument('--quiet', action='store_true')


def main():
    # setup_logzero(logging.getLogger('sqlalchemy.engine'), level=logging.DEBUG)
    p = argparse.ArgumentParser('promnesia server', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    setup_parser(p)
    args = p.parse_args()
    run(port=args.port, config=args.config, quiet=args.quiet)


if __name__ == '__main__':
    main()



#!/usr/bin/env python3

import json
import datetime
import codecs
import sys
import os
import time
import ast
import getpass
# 3rd party dependencies:
import requests
import pytz

# Stand-alone daemon managed by Telegraf.
# Up to date with: Monitoring server API version January 2019
# Note: document does not correspond to reality
#
# Automatically fetches all sites connected to the account/API key
# Does not support the whole API, just what is required.
#
SETTING_API_KEY = ''
SETTING_SITE_USERNAME = ''
SETTING_SITE_PW = ''
# Args:
# - 'history' to scrape the past history from the cloud
# - 'debug' to run the update loop once
# - No args to run the daily loop

# -----------------------------------------------------------------------

# SolarEdge does not expose all information they have through their API,
# hence some of it has to be scraped from their website. As a bonus
# their API is rate limited but their website is not (or a much higher limit?).

BASE_SITE_PANELS_URL = 'https://monitoring.solaredge.com/solaredge-web/p/playbackData'
SITE_LOGIN_URL = 'https://monitoring.solaredge.com/solaredge-apigw/api/login'
BASE_API_URL = 'https://monitoringapi.solaredge.com'
REQUEST_TIMEOUT = 60
SITE_COOKIE_FILE = 'solaredge.com.cookies'
LAST_SUCCESSFUL_UPDATE_FILE = 'lastupdated'
INSTALLATION_INFO_FILE = 'installinfo'
SITE_IDS = []
SITES = ''  # Same as SITE_IDS but as string
SERIALS = {}
SITE_TIMEZONES = {}
HAS_OPTIMIZERS = {}
LAST_UPDATES = {}
HOME_DIR = ''
HISTORY_SCRAPER_MAX_API_CALLS = 280  # Limit is 300/day, take some margin
# Data is updated once a day at this interval. Assumed to be at the ~end of the day.
UPDATE_INTERVAL_HOUR = 23
UPDATE_INTERVAL_MIN = 50

# ------------------------------ Utils -----------------------------------------


# Write to Telegraf
def flush():
    sys.stdout.flush()
    sys.stderr.flush()


def flush_and_exit(code: int):
    flush()
    exit(code)


def format_datetime_url(date: datetime.datetime):
    return date.strftime('%Y-%m-%d %H:%M:%S')


def format_date_url(date: datetime.datetime):
    return date.strftime('%Y-%m-%d')


def wh_unit_to_multiplier(unit: str):
    first = unit[:1]
    if first == 'G':
        return 1000000000.0
    if first == 'M':
        return 1000000.0
    if first == 'k':
        return 1000.0
    return 1.0


def print_err(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


# In ns
def to_unix_timestamp(date: str):
    return f"{int(datetime.datetime.strptime(date, '%Y-%m-%d %H:%M:%S').timestamp())}000000000"


def safe_str_to_float(num: str):
    # A better way is to know the used locale and convert, alas it is not exposed by SolarEdge cloud.
    if num.find(",") != -1 and num.find(
            "."):  # Locals that use 1.000,00 for 1000
        num = num.replace(".", "")
    return float(num.replace(",", "."))


def get_date_intervals(start: datetime.datetime, end: datetime.datetime,
                       maxDays: int):
    intervals = []

    days = (end - start).days
    prev = start
    while True:
        if days <= (maxDays + 1):
            intervals.append((prev, prev + datetime.timedelta(days=days)))
            break

        days -= maxDays + 1  # +1 because we want no overlaps between the intervals

        next = prev + datetime.timedelta(days=maxDays)
        intervals.append((prev, next))
        prev = next + datetime.timedelta(days=1)

    return intervals


# Because Python, datetime is not supported by literal_eval(...)
# Source: https://stackoverflow.com/questions/4235606/python-ast-literal-eval-and-datetime
def parse_datetime_dict(astr, debug=False):
    try:
        tree = ast.parse(astr)
    except SyntaxError:
        raise ValueError(astr)
    for node in ast.walk(tree):
        if isinstance(node,
                      (ast.Module, ast.Expr, ast.Dict, ast.Str, ast.Attribute,
                       ast.Num, ast.Name, ast.Load, ast.Tuple)):
            continue
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'datetime'):
            continue
        if debug:
            attrs = [attr for attr in dir(node) if not attr.startswith('__')]
            print(node)
            for attrname in attrs:
                print('    {k} ==> {v}'.format(k=attrname,
                                               v=getattr(node, attrname)))
        raise ValueError(astr)
    return eval(astr)


def format_L_data(data, label: str):
    if 'cosPhi' in data:
        return f",I_{label}_AC_Voltage={data['acVoltage']},I_{label}_AC_Current={data['acCurrent']},I_{label}_AC_PF={data['cosPhi']},I_{label}_AC_Freq={data['acFrequency']},I_{label}_AC_VAR={data['reactivePower']},I_{label}_AC_VA={data['apparentPower']},I_{label}_AC_Power={data['activePower']}"
    else:
        return f",I_{label}_AC_Voltage={data['acVoltage']},I_{label}_AC_Current={data['acCurrent']},I_{label}_AC_Freq={data['acFrequency']},I_{label}_AC_VAR={data['reactivePower']},I_{label}_AC_VA={data['apparentPower']},I_{label}_AC_Power={data['activePower']}"



# --------------------------- Main() helpers ------------------------------


# Should only be called once
def initialize_home_dir():
    global HOME_DIR

    # By default the env will be set to the session user (alarm's home) which the telegraf user
    # has no access too. Hence when writing files a full path must be used.
    HOME_DIR = os.path.expanduser(
        '~' + getpass.getuser()
    )  # get the home of the telegraf user (same as the location of the script)


# Should only be called once
def initialize_installation_info():
    global SITES, SERIALS, SITE_TIMEZONES, SITE_IDS, HAS_OPTIMIZERS

    # Check if the info is already cached
    if os.path.exists(os.path.join(HOME_DIR, INSTALLATION_INFO_FILE)):
        with open(os.path.join(HOME_DIR, INSTALLATION_INFO_FILE), "r") as f:
            data = ast.literal_eval(f.read())
            SITE_IDS = data['SITE_IDS']
            SITES = ','.join(SITE_IDS)
            SERIALS = data['SERIALS']
            SITE_TIMEZONES = data['SITE_TIMEZONES']
            HAS_OPTIMIZERS = data['HAS_OPTIMIZERS']
            # TODO TBD should check for equipment updates (there is an API available)
            return True

    # Get sites
    r = requests.get(f"{BASE_API_URL}/sites/list.json",
                     {'api_key': SETTING_API_KEY},
                     timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        print_err(f"SolarEdge Cloud: Sites: HTTP {r.status_code} : {r.url}")
        return False

    # Parse response
    for site in r.json()['sites']['site']:
        site_id = str(site['id'])
        SITE_IDS.append(site_id)
        SITE_TIMEZONES[site_id] = site['location']['timeZone']
        HAS_OPTIMIZERS[site_id] = site['type'].find("Optimizers") != -1
    SITES = ','.join(SITE_IDS)

    # Get serials
    # Note: 1 call per site
    for site in SITE_IDS:
        r = requests.get(f"{BASE_API_URL}/site/{site}/inventory",
                         {'api_key': SETTING_API_KEY},
                         timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            print_err(
                f"SolarEdge Cloud: Inventory: HTTP {r.status_code} : {r.url}")
            return False

        # Parse response
        serials = []
        for inverter in r.json()['Inventory']['inverters']:
            serials.append(inverter['SN'])
        SERIALS[site] = serials

    # Cache data
    with open(os.path.join(HOME_DIR, INSTALLATION_INFO_FILE), "w") as f:
        f.write(
            repr({
                'SITE_IDS': SITE_IDS,
                'SERIALS': SERIALS,
                'SITE_TIMEZONES': SITE_TIMEZONES,
                'HAS_OPTIMIZERS': HAS_OPTIMIZERS
            }))

    return True


# Should only be called once
def initialize_last_updated():
    global LAST_UPDATES

    if os.path.exists(os.path.join(HOME_DIR, LAST_SUCCESSFUL_UPDATE_FILE)):
        with open(os.path.join(HOME_DIR, LAST_SUCCESSFUL_UPDATE_FILE),
                  "r") as f:
            LAST_UPDATES = parse_datetime_dict(f.read())
    else:
        # Well it must be intialized at something
        # Note: will not auto scrape full history
        resetDate = datetime.datetime.now().replace(
            hour=UPDATE_INTERVAL_HOUR, minute=UPDATE_INTERVAL_MIN,
            second=0) - datetime.timedelta(days=1)
        site_dict = {}
        for site in SITE_IDS:
            site_dict[site] = resetDate
        LAST_UPDATES['power'] = site_dict.copy()
        LAST_UPDATES['energy'] = site_dict.copy()
        LAST_UPDATES['data'] = site_dict.copy()
        LAST_UPDATES['playback'] = site_dict.copy()


def ensure_logged_in(session: requests.Session, function):
    if os.path.exists(os.path.join(HOME_DIR, SITE_COOKIE_FILE)):
        with open(os.path.join(HOME_DIR, SITE_COOKIE_FILE), 'r') as f:
            session.cookies.update(
                requests.utils.cookiejar_from_dict(json.load(f)))
            response = function()
            if response.status_code == 200:
                return response

    # Log in again
    session.post(SITE_LOGIN_URL,
                 headers={"Content-Type": "application/x-www-form-urlencoded"},
                 data={
                     "j_username": SETTING_SITE_USERNAME,
                     "j_password": SETTING_SITE_PW
                 })
    with open(os.path.join(HOME_DIR, SITE_COOKIE_FILE), 'w') as f:
        json.dump(requests.utils.dict_from_cookiejar(session.cookies), f)

    return function()


def update_all_data(endTime: datetime.datetime):
    playbackTimeStamps = LAST_UPDATES['playback']
    # for site in SITE_IDS:
    #     if HAS_OPTIMIZERS[site]:
    #         nr_days = max((endTime - playbackTimeStamps[site]).days,
    #                       7)  # API only supports up to 1 week history
    #         days = [0]
    #         if nr_days != 1:
    #             days = list(range(-nr_days, 0, 1))
    #         if get_playback_data_site(days, site):
    #             playbackTimeStamps[site] = endTime
    # powerTimeStamps = LAST_UPDATES['power']
    # for site in SITE_IDS:
    #     if get_power_api(site, powerTimeStamps[site], endTime):
    #         powerTimeStamps[site] = endTime
    # energyTimeStamps = LAST_UPDATES['energy']
    # for site in SITE_IDS:
    #     if get_energy_api(site, energyTimeStamps[site], endTime):
    #         energyTimeStamps[site] = endTime
    dataTimeStamps = LAST_UPDATES['data']
    for site in SITE_IDS:
        if get_data_api(site, dataTimeStamps[site], endTime):
            dataTimeStamps[site] = endTime

    flush()

    # Persist last successful update
    with open(os.path.join(HOME_DIR, LAST_SUCCESSFUL_UPDATE_FILE), "w") as f:
        f.write(repr(LAST_UPDATES))


# --------------------------- Data gathering ----------------------------

# API


def get_power_api(site: str, startTime: datetime, endTime: datetime):
    r = requests.get(f"{BASE_API_URL}/site/{site}/powerDetails.json", {
        'startTime': format_datetime_url(startTime),
        'endTime': format_datetime_url(endTime),
        'api_key': SETTING_API_KEY
    },
        timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        print_err(f"SolarEdge Cloud: Power: HTTP {r.status_code} : {r.url}")
        return False

    # Parse request
    json = r.json()
    multiplier = wh_unit_to_multiplier(json['powerDetails']['unit'])
    for meter in json['powerDetails']['meters']:
        type = meter['type'].lower()
        for point in meter['values']:
            if 'value' in point:
                print(
                    f'power,site={site},type={type} w={float(point["value"]) * multiplier} {to_unix_timestamp(point["date"])}',
                    flush=False)
    return True


def get_energy_api(site: str, startTime: datetime, endTime: datetime):
    r = requests.get(f"{BASE_API_URL}/site/{site}/energyDetails.json", {
        'timeUnit': 'QUARTER_OF_AN_HOUR',
        'startTime': format_datetime_url(startTime),
        'endTime': format_datetime_url(endTime),
        'api_key': SETTING_API_KEY
    },
        timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        print_err(f"SolarEdge Cloud: Energy: HTTP {r.status_code} : {r.url}")
        return False

    # Parse request
    json = r.json()
    multiplier = wh_unit_to_multiplier(json['energyDetails']['unit'])
    for meter in json['energyDetails']['meters']:
        type = meter['type'].lower()
        for point in meter['values']:
            if 'value' in point:
                print(
                    f'energy,site={site},type={type} wh={float(point["value"]) * multiplier} {to_unix_timestamp(point["date"])}',
                    flush=False)
    return True


# This data is similar as what can be read from modbus
def get_data_api(site: str, startTime: datetime, endTime: datetime):
    for serial in SERIALS[site]:
        r = requests.get(f"{BASE_API_URL}/equipment/{site}/{serial}/data", {
            'startTime': format_datetime_url(startTime),
            'endTime': format_datetime_url(endTime),
            'api_key': SETTING_API_KEY
        },
            timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            print_err(f"SolarEdge Cloud: Data: HTTP {r.status_code} : {r.url}")
            # Note: might cause double data if the first call does not fail (prevous sites wil be remeasured next call)
            return False

        # Parse request
        for value in r.json()['data']['telemetries']:
            date = value['date']
            # Note: not all data is logged; see Json/API for all available options
            conditionalData = ''
            dcVoltage = value['dcVoltage']
            if dcVoltage is not None:
                conditionalData += f",I_DC_Voltage={dcVoltage}"
            if 'L1Data' in value:
                conditionalData += format_L_data(value['L1Data'], 'L1')
            if 'L2Data' in value:
                conditionalData += format_L_data(value['L2Data'], 'L2')
            if 'L3Data' in value:
                conditionalData += format_L_data(value['L3Data'], 'L3')
            from pprint import pprint as pp
            pp(value)
    return True


# Scrape website


# Based on: https://gist.github.com/dragoshenron/0920411a2f3e53c214be0a26f51c53e2
# Note: only available if you have optimizers
def get_playback_data_site(days, site: str):
    PANELS_DAILY_DATA = '4'
    PANELS_WEEKLY_DATA = '5'
    timeUnit = PANELS_WEEKLY_DATA if len(
        days) > 1 or days[0] != 0 else PANELS_DAILY_DATA

    session = requests.session()
    panels = ensure_logged_in(
        session, lambda: session.post(
            BASE_SITE_PANELS_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRF-TOKEN": session.cookies["CSRF-TOKEN"]
            },
            data={
                "fieldId": site,
                "timeUnit": timeUnit
            },
            timeout=REQUEST_TIMEOUT))
    if panels.status_code != 200:
        print_err(
            f"SolarEdge Cloud: Playback: HTTP {panels.status_code} : {panels.url}"
        )
        return

    # Correct their JSON
    response = panels.content.decode("utf-8").replace('\'', '"').replace(
        'Array', '').replace('key', '"key"').replace('value', '"value"')
    response = response.replace('timeUnit', '"timeUnit"').replace(
        'fieldData', '"fieldData"').replace('reportersData', '"reportersData"')
    response = json.loads(response)
    for date, sids in response["reportersData"].items():
        timestamp = str(int((pytz.timezone(SITE_TIMEZONES[site]).localize(
            datetime.datetime.strptime(date,
                                       '%a %b %d %H:%M:%S GMT %Y')).astimezone(
                                           pytz.utc)).timestamp())) + "000000000"
        for values in sids.values():  # SID's (key) are meaningless
            for panel in values:
                if panel['value'] != "0":  # No measurement
                    print(
                        f'panel,site={site},id={panel["key"]} w={float(safe_str_to_float(panel["value"]))} {timestamp}',
                        flush=False)
    return


# TODO
# Get the logical layout: 'https://monitoring.solaredge.com/solaredge-apigw/api/sites/{site}/layout/logical'
# Can be used to fetch more info per panel: optimizer: V, general V, Current and Power.
# However this has to be queried manually every 15 minutes (cloud update interval) and not all panels update at the same time so some bookkeeping is required.
# As the current script only updates once a day this has been omitted. Unfortunately this data is not included in the playback data.

# ------------------------- History Scraper ------------------------------


def reduce_and_check(nr_calls: int):
    nr_calls -= 1
    if nr_calls == 0:
        nr_calls = HISTORY_SCRAPER_MAX_API_CALLS
        now = datetime.datetime.now()
        time.sleep(now.replace(hour=24, minute=0, second=0) -
                   now)  # Wait till next day
    return nr_calls


def get_production_duration():
    r = requests.get(f"{BASE_API_URL}/sites/{SITES}/dataPeriod.json",
                     {'api_key': SETTING_API_KEY},
                     timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        print_err(
            f"SolarEdge Cloud: DataPeriod: HTTP {r.status_code} : {r.url}")
        return

    # Parse request
    json = r.json()
    ranges = {}
    for site in json['datePeriodList']['siteEnergyList']:
        startDate = site['dataPeriod']['startDate']
        endDate = site['dataPeriod']['endDate']
        if startDate is not None and endDate is not None:
            ranges[str(site['siteId'])] = (datetime.datetime.strptime(
                startDate,
                '%Y-%m-%d'), datetime.datetime.strptime(endDate, '%Y-%m-%d'))
    return ranges


def scrape_full_history():
    RETRY_SLEEP = 60.0
    INTERVAL_SLEEP = 1.0

    last_updates = LAST_UPDATES.copy()
    ranges = get_production_duration()
    remaining_API_calls = reduce_and_check(HISTORY_SCRAPER_MAX_API_CALLS)

    for site in SITE_IDS:
        ranges = ranges[site]

        # API limited to 1 month time range (apparently 1 month == 28 days)
        # Assumption: not called between midnight and UPDATE_INTERVAL
        powerLastUpdates = last_updates['power']
        for month in get_date_intervals(ranges[0],
                                        min(powerLastUpdates[site], ranges[1]),
                                        28):
            remaining_API_calls = reduce_and_check(remaining_API_calls)
            while not get_power_api(site, month[0], month[1]):
                remaining_API_calls = reduce_and_check(remaining_API_calls)
                time.sleep(RETRY_SLEEP)
            time.sleep(INTERVAL_SLEEP)
        flush()

        # API limited to 1 month time range
        energyLastUpdates = last_updates['energy']
        for month in get_date_intervals(
                ranges[0], min(energyLastUpdates[site], ranges[1]), 28):
            remaining_API_calls = reduce_and_check(remaining_API_calls)
            while not get_energy_api(site, month[0], month[1]):
                remaining_API_calls = reduce_and_check(remaining_API_calls)
                time.sleep(RETRY_SLEEP)
            time.sleep(INTERVAL_SLEEP)
        flush()

        # API limited to 1 week time range
        dataLastUpdates = last_updates['data']
        for week in get_date_intervals(ranges[0],
                                       min(dataLastUpdates[site], ranges[1]),
                                       7):
            remaining_API_calls = reduce_and_check(remaining_API_calls)
            while not get_data_api(site, week[0], week[1]):
                remaining_API_calls = reduce_and_check(remaining_API_calls)
                time.sleep(RETRY_SLEEP)
            time.sleep(INTERVAL_SLEEP)
        flush()


# -----------------------------------------------------------------------
# Main()
# -----------------------------------------------------------------------

initialize_home_dir()

if not initialize_installation_info():
    print_err('Failed to initialize installation info, exiting.')
    flush_and_exit(1)

initialize_last_updated()

if len(sys.argv) > 2:
    print_err(f'Unknown CLI arguments {str(sys.argv)}, existing.')
    flush_and_exit(1)

if len(sys.argv) == 2:
    # History scrape loop
    if sys.argv[1] == 'history':
        scrape_full_history()
        flush_and_exit(0)
    # Debug loop
    elif sys.argv[1] == 'debug':
        update_all_data(datetime.datetime.now().replace(
            hour=UPDATE_INTERVAL_HOUR, minute=UPDATE_INTERVAL_MIN))
        flush_and_exit(0)

    print_err(f'Unknown CLI argument {sys.argv[1]}, existing.')
    flush_and_exit(1)

# Daily update loop
while True:
    # Always run at the end of the day ~midnight to get the most accurate daily data.
    # Assumption: it will be dark by midnight
    now = datetime.datetime.now()
    nextUpdate = now.replace(hour=UPDATE_INTERVAL_HOUR,
                             minute=UPDATE_INTERVAL_MIN,
                             second=0)
    if now.hour >= UPDATE_INTERVAL_HOUR and now.minute >= UPDATE_INTERVAL_MIN:
        nextUpdate += datetime.timedelta(days=1)
    time.sleep((nextUpdate - datetime.datetime.now()).total_seconds())

    update_all_data(datetime.datetime.now().replace(hour=UPDATE_INTERVAL_HOUR,
                                                    minute=UPDATE_INTERVAL_MIN,
                                                    second=0))
flush_and_exit(0)

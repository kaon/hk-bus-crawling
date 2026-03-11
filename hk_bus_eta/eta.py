"""
This module implements the main functionality of hk-bus-eta

Author: Chun Law
Github: https://github.com/chunlaw
"""

__author__ = "Chun Law"
__email__ = "chunlaw@rocketmail.com"
__status__ = "production"

import asyncio
import hashlib
import re
import time
from datetime import datetime, timezone

import httpx


REQUEST_TIMEOUT_SECONDS = 15
RATE_LIMIT = 10
MAX_RETRIES = 3
RETRY_BACKOFF_MAX = 4
CACHE_TTL_SECONDS = 15


def get_platform_display(plat, lang):
  number = int(plat) if isinstance(plat, str) else plat
  if number < 0 or number > 20:
    return ("Platform {}" if lang == "en" else "{}號月台").format(number)
  if number == 0:
    return "⓿"
  if number > 10:
    return chr(9451 + (number - 11))
  return chr(10102 + (number - 1))


class HKEta:
  holidays = None
  route_list = None
  stop_list = None
  stop_map = None

  def __init__(self):
    self._client = httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS), follow_redirects=True)
    self._semaphore = None  # created lazily — must be bound to a running event loop
    self._cache = {}  # {key: (expiry_timestamp, data)}

  @classmethod
  async def create(cls):
    instance = cls()
    await instance._load_data()
    return instance

  async def __aenter__(self):
    return self

  async def __aexit__(self, *args):
    await self.close()

  async def close(self):
    await self._client.aclose()

  async def _get_semaphore(self):
    if self._semaphore is None:
      self._semaphore = asyncio.Semaphore(RATE_LIMIT)
    return self._semaphore

  async def _request(self, method, url, **kwargs):
    semaphore = await self._get_semaphore()
    backoff = 1
    for attempt in range(MAX_RETRIES):
      try:
        async with semaphore:
          r = await self._client.request(method, url, **kwargs)
        if r.status_code == 200:
          return r
        elif r.status_code in (429, 502, 504, 403):
          if attempt < MAX_RETRIES - 1:
            await asyncio.sleep(min(backoff, RETRY_BACKOFF_MAX))
            backoff *= 2
          else:
            r.raise_for_status()
        else:
          r.raise_for_status()
      except (httpx.TimeoutException, httpx.NetworkError):
        if attempt < MAX_RETRIES - 1:
          await asyncio.sleep(min(backoff, RETRY_BACKOFF_MAX))
          backoff *= 2
        else:
          raise
    raise Exception("Max retries exceeded for {}".format(url))

  def _cache_get(self, key):
    entry = self._cache.get(key)
    if entry and time.time() < entry[0]:
      return entry[1]
    return None

  def _cache_set(self, key, data):
    self._cache[key] = (time.time() + CACHE_TTL_SECONDS, data)

  async def _load_data(self):
    md5_resp, data_resp = await asyncio.gather(
        self._client.get("https://hkbus.github.io/hk-bus-crawling/routeFareList.md5"),
        self._client.get("https://hkbus.github.io/hk-bus-crawling/routeFareList.min.json"),
    )
    md5 = md5_resp.text.strip().split()[0].lower()
    m = hashlib.md5()
    m.update(data_resp.content)
    if md5 != m.hexdigest():
      raise Exception("Error in accessing hk-eta-db, md5sum not match")
    db = data_resp.json()
    self.holidays, self.route_list, self.stop_list, self.stop_map = db[
        "holidays"], db["routeList"], db["stopList"], db["stopMap"]

  # 0-indexed seq
  async def getEtas(self, route_id, seq, language):
    routeEntry = self.route_list[route_id]
    route, stops, bound = routeEntry['route'], routeEntry['stops'], routeEntry['bound']
    dest, service_type, co, nlb_id, gtfs_id = routeEntry['dest'], routeEntry[
        'serviceType'], routeEntry['co'], routeEntry["nlbId"], routeEntry['gtfsId']

    tasks = []
    if "kmb" in co and "kmb" in stops and seq < len(stops["kmb"]):
      tasks.append(self.kmb(
          route=route, stop_id=stops["kmb"][seq], bound=bound["kmb"],
          seq=seq, co=co, service_type=service_type
      ))
    if "ctb" in co and "ctb" in stops and seq < len(stops["ctb"]):
      tasks.append(self.ctb(
          stop_id=stops['ctb'][seq], route=route, bound=bound['ctb'], seq=seq
      ))
    if "nlb" in co and "nlb" in stops and seq < len(stops['nlb']):
      tasks.append(self.nlb(stop_id=stops['nlb'][seq], nlb_id=nlb_id))
    if "lrtfeeder" in co and "lrtfeeder" in stops and seq < len(stops['lrtfeeder']):
      tasks.append(self.lrtfeeder(
          stop_id=stops['lrtfeeder'][seq], route=route, language=language
      ))
    if "mtr" in co and "mtr" in stops and seq < len(stops['mtr']):
      tasks.append(self.mtr(
          stop_id=stops['mtr'][seq], route=route, bound=bound["mtr"]
      ))
    if "lightRail" in co and "lightRail" in stops and seq < len(stops['lightRail']):
      tasks.append(self.lightrail(
          stop_id=stops['lightRail'][seq], route=route, dest=dest
      ))
    if "gmb" in co and "gmb" in stops and seq < len(stops["gmb"]):
      tasks.append(self.gmb(
          stop_id=stops["gmb"][seq], gtfs_id=gtfs_id, seq=seq, bound=bound["gmb"]
      ))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    _etas = []
    for r in results:
      if not isinstance(r, Exception):
        _etas.extend(r)
    return _etas

  async def kmb(self, stop_id, route, seq, service_type, co, bound):
    r = await self._request(
        "GET",
        "https://data.etabus.gov.hk/v1/transport/kmb/eta/{}/{}/{}".format(
            stop_id, route, service_type)
    )
    data = r.json()['data']
    data = list(filter(lambda e: 'eta' in e and e['dir'] == bound, data))
    if len(data) == 0:
      return []
    data.sort(key=lambda e: abs(seq - e['seq']))
    data = [e for e in data if e['seq'] == data[0]['seq']]
    data = list(filter(lambda e: len(co) > 1 or service_type ==
                e['service_type'] or e['seq'] == seq + 1, data))
    return [{
        "eta": e['eta'],
        "remark": {
            "zh": e['rmk_tc'],
            "en": e['rmk_en']
        },
        "co": "kmb"
    } for e in data]

  async def ctb(self, stop_id, route, bound, seq):
    r = await self._request(
        "GET",
        "https://rt.data.gov.hk/v2/transport/citybus/eta/CTB/{}/{}".format(stop_id, route)
    )
    data = r.json()['data']
    data = list(filter(lambda e: 'eta' in e and e['dir'] in bound, data))
    if len(data) == 0:
      return []
    data.sort(key=lambda e: abs(seq - e['seq']))
    data = [e for e in data if e['seq'] == data[0]['seq']]
    return [{
        "eta": e['eta'],
        "remark": {
            "zh": e['rmk_tc'],
            "en": e['rmk_en']
        },
        "co": "ctb"
    } for e in data]

  async def nlb(self, stop_id, nlb_id):
    try:
      r = await self._request(
          "POST",
          "https://rt.data.gov.hk/v1/transport/nlb/stop.php?action=estimatedArrivals",
          json={
              "routeId": nlb_id,
              "stopId": stop_id,
              "language": "zh"},
          headers={"Content-Type": "text/plain"}
      )
      data = r.json()["estimatedArrivals"]
      data = list(filter(lambda e: 'estimatedArrivalTime' in e, data))
      return [{
          "eta": e['estimatedArrivalTime'].replace(' ', 'T') + ".000+08:00",
          "remark": {"zh": "", "en": ""},
          "co": "nlb"
      } for e in data]
    except Exception:
      return []

  async def lrtfeeder(self, stop_id, route, language):
    cache_key = ("lrtfeeder", route, language)
    cached = self._cache_get(cache_key)
    if cached is None:
      r = await self._request(
          "POST",
          "https://rt.data.gov.hk/v1/transport/mtr/bus/getSchedule",
          json={"language": language, "routeName": route},
          headers={"Content-Type": "application/json"}
      )
      cached = r.json()['busStop']
      self._cache_set(cache_key, cached)

    data = list(filter(lambda e: e["busStopId"] == stop_id, cached))
    ret = []
    for buses in data:
      for bus in buses['bus']:
        remark = ""
        if bus["busRemark"] is not None:
          remark = bus["busRemark"]
        elif bus["isScheduled"] == 1:
          remark = "Scheduled" if language == "en" else "預定班次"
        delta_second = int(bus["departureTimeInSecond"] if bus['arrivalTimeInSecond']
                           == "108000" else bus["arrivalTimeInSecond"])
        dt = datetime.fromtimestamp(time.time() + delta_second + 8 * 3600)
        ret.append({
            "eta": dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "remark": {language: remark},
            "co": "lrtfeeder"
        })
    return ret

  async def mtr(self, stop_id, route, bound):
    r = await self._request(
        "GET",
        "https://rt.data.gov.hk/v1/transport/mtr/getSchedule.php?line={}&sta={}".format(
            route, stop_id)
    )
    res = r.json()
    data, status = res["data"], res["status"]
    if status == 0:
      return []
    route_key = "{}-{}".format(route, stop_id)
    if route_key not in data:
      return []
    direction = "UP" if str(bound).upper().startswith("U") else "DOWN"
    if direction not in data[route_key]:
      return []
    return [{
        "eta": e["time"].replace(" ", "T") + "+08:00",
        "remark": {
            "zh": get_platform_display(e["plat"], "zh"),
            "en": get_platform_display(e["plat"], "en")
        },
        "co": "mtr"
    } for e in data[route_key][direction]]

  async def lightrail(self, stop_id, route, dest):
    r = await self._request(
        "GET",
        "https://rt.data.gov.hk/v1/transport/mtr/lrt/getSchedule?station_id={}".format(stop_id[2:])
    )
    platform_list = r.json()["platform_list"]
    ret = []
    for platform in platform_list:
      route_list, platform_id = platform["route_list"], platform["platform_id"]
      for e in route_list:
        route_no, dest_ch, dest_en, stop, time_en = e["route_no"], e[
            "dest_ch"], e["dest_en"], e["stop"], e["time_en"]
        if route == route_no and (
                dest_ch == dest["zh"] or "Circular" in dest_en) and stop == 0:
          if time_en.lower() == "arriving" or time_en.lower() == "departing" or time_en == "-":
            waitTime = 0
          else:
            match = re.search(r'\d+', time_en)
            waitTime = int(match.group()) if match else 0
          dt = datetime.fromtimestamp(time.time() + waitTime + 8 * 3600)
          ret.append({
              "eta": dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
              "remark": {
                  "zh": get_platform_display(platform_id, "zh"),
                  "en": get_platform_display(platform_id, "en")
              },
              "co": "lightrail"
          })
    return ret

  async def gmb(self, gtfs_id, stop_id, bound, seq):
    r = await self._request(
        "GET",
        "https://data.etagmb.gov.hk/eta/route-stop/{}/{}".format(gtfs_id, stop_id)
    )
    data = r.json()["data"]
    data = list(
        filter(
            lambda e: (
                e['route_seq'] == 1 and bound == "O") or (
                e['route_seq'] == 2 and bound == "I"),
            data))
    data = list(filter(lambda e: e["stop_seq"] == seq + 1, data))
    ret = []
    for e in data:
      etas = e["eta"]
      for eta in etas:
        ret.append({
            "eta": eta["timestamp"],
            "remark": {
                "zh": eta["remarks_tc"],
                "en": eta["remarks_en"],
            },
            "co": "gmb"
        })
    return ret


if __name__ == "__main__":
  async def main():
    async with await HKEta.create() as hketa:
      route_ids = list(hketa.route_list.keys())
      print(route_ids[0:10])
  asyncio.run(main())

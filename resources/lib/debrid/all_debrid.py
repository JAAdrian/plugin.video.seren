import time
from functools import cached_property
from functools import wraps
from urllib import parse

import xbmc
import xbmcgui

from resources.lib.database.cache import use_cache
from resources.lib.modules.globals import g

AD_AUTH_KEY = "alldebrid.apikey"
AD_ENABLED_KEY = "alldebrid.enabled"


def alldebrid_guard_response(func):
    @wraps(func)
    def wrapper(*args, **kwarg):
        import requests

        try:
            response = func(*args, **kwarg)
            if response.status_code in [200, 201]:
                return response

            if response.status_code == 429:
                g.log('Alldebrid Throttling Applied, Sleeping for 1 seconds')
                xbmc.sleep(1 * 1000)
                response = func(*args, **kwarg)
                if response is not None and response.status_code in [200, 201]:
                    return response

            try:
                err_body = response.json()
            except Exception:
                err_body = {}
            ad_error = err_body.get("error", {}) if isinstance(err_body, dict) else {}
            g.log(
                f"AllDebrid returned a {response.status_code} "
                f"({AllDebrid.http_codes.get(response.status_code, 'Unknown')}): "
                f"{ad_error.get('code', '')} {ad_error.get('message', '')} "
                f"while requesting {response.url}",
                "warning",
            )
            return None
        except requests.exceptions.ConnectionError:
            return None
        except Exception:
            xbmcgui.Dialog().notification(g.ADDON_NAME, g.get_language_string(30024).format("AllDebrid"))
            raise

    return wrapper


class AllDebrid:
    """AllDebrid API wrapper.

    Documentation for deprecated v4.0 `magnet/status`:
    https://docs.alldebrid.com/#v4-magnet-status

    Documentation for v4.1 `magnet/status`:
    https://docs.alldebrid.com/#get-status
    """
    base_url = "https://api.alldebrid.com/v4.1/"

    http_codes = {
        200: "Success",
        400: "Bad Request, The request was unacceptable, often due to missing a required parameter",
        401: "Unauthorized",
        404: "Not Found, Api endpoint doesn't exist",
        500: "Internal Server Error",
        502: "Bad Gateway",
        503: "Service Unavailable",
        504: "Gateway Timeout",
        524: "Internal Server Error",
    }

    def __init__(self):
        self.agent_identifier = g.ADDON_NAME
        self.apikey = g.get_setting(AD_AUTH_KEY)

    @cached_property
    def session(self):
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3 import Retry

        session = requests.Session()
        retries = Retry(total=5, backoff_factor=0.1, status_forcelist=[429, 500, 502, 503, 504])
        session.mount("https://", HTTPAdapter(max_retries=retries, pool_maxsize=100))
        return session

    @alldebrid_guard_response
    def get(self, url, **params):
        if not g.get_bool_setting(AD_ENABLED_KEY):
            return

        headers = {}
        if not params.pop("reauth", None) and self.apikey:
            headers["Authorization"] = f"Bearer {self.apikey}"

        return self.session.get(
            parse.urljoin(self.base_url, url),
            params=params,
            headers=headers,
            timeout=10,
        )

    def get_json(self, url, **params):
        return self._extract_data(self.get(url, **params).json())

    @alldebrid_guard_response
    def post(self, url, post_data=None, **params):
        if not g.get_bool_setting(AD_ENABLED_KEY) or not self.apikey:
            return
        headers = {"Authorization": f"Bearer {self.apikey}"}
        return self.session.post(
            parse.urljoin(self.base_url, url),
            data=post_data,
            params=params,
            headers=headers,
            timeout=10,
        )

    def post_json(self, url, post_data=None, **params):
        response = self.post(url, post_data, **params)
        if response is None:
            return None
        return self._extract_data(response.json())

    def _extract_data(self, response):
        return response["data"] if "data" in response else response

    def auth(self):
        from resources.lib.modules.qr_auth import auth_progress_percent, open_auth_dialog

        resp = self.get_json("pin/get", reauth=True)
        expiry = pin_ttl = int(resp["expires_in"])
        auth_complete = False
        auth_check = None
        progress = open_auth_dialog(
            f"{g.ADDON_NAME}: {g.get_language_string(30334)}",
            resp["base_url"],
            user_code=resp["pin"],
        )
        try:
            # AllDebrid needs a short delay before polling the pin.
            xbmc.sleep(5 * 1000)

            while not auth_complete and expiry > 0 and not progress.iscanceled():
                auth_check = self.get_json("pin/check", check=resp["check"], pin=resp["pin"])
                if auth_check["activated"]:
                    auth_complete = True
                    break
                expiry = int(auth_check["expires_in"])
                progress.update(auth_progress_percent(expiry, pin_ttl))
                xbmc.sleep(1 * 1000)

            if auth_complete and not progress.iscanceled() and auth_check is not None:
                g.set_setting(AD_AUTH_KEY, auth_check["apikey"])
                self.apikey = auth_check["apikey"]
                self.store_user_info()
        finally:
            progress.close()

        if auth_complete:
            xbmcgui.Dialog().ok(g.ADDON_NAME, f"AllDebrid {g.get_language_string(30020)}")
        else:
            return

    def get_user_info(self):
        return self._extract_data(self.get_json("user")).get("user", {})

    def store_user_info(self):
        user_information = self.get_user_info()
        if user_information is not None:
            g.set_setting("alldebrid.username", user_information["username"])
            g.set_setting("alldebrid.premiumstatus", self.get_account_status().title())

    def upload_magnet(self, magnet_hash):
        return self.post_json("magnet/upload", magnet=[magnet_hash])

    @use_cache(1)
    def update_relevant_hosters(self):
        return self.get_json("hosts")

    def get_hosters(self, hosters):
        host_list = self.update_relevant_hosters()
        if host_list is not None:
            hosters["premium"]["all_debrid"] = [
                (d, d.split(".")[0])
                for l in host_list["hosts"].values()
                if "status" in l and l["status"]
                for d in l["domains"]
            ]
        else:
            g.log_stacktrace()
            hosters["premium"]["all_debrid"] = []

    def resolve_hoster(self, url):
        resolve = self.post_json("link/unlock", link=url)
        return resolve["link"]

    def magnet_status(self, magnet_id):
        if magnet_id:
            resp = self.post_json("magnet/status", id=magnet_id)
            if resp is None:
                return None
            magnets = resp.get("magnets")
            if magnets is None:
                return None
            if isinstance(magnets, dict):
                return magnets
            if isinstance(magnets, list):
                return magnets[0] if magnets else None
            return None
        return self.get_json("magnet/status")

    def saved_magnets(self):
        resp = self.get_json("magnet/status")
        return resp.get("magnets") if isinstance(resp, dict) else resp

    def check_hash(self, hash_value):
        """Probe whether a hash is servable on AllDebrid.

        Mirrors RealDebrid's `check_hash` semantics: returns a non-empty dict
        keyed by hash iff the magnet is already Ready. Otherwise the magnet
        is deleted and an empty dict is returned, signalling the source is
        unusable.

        Optimized: first checks `saved_magnets()` to avoid a needless upload
        when the hash is already cached. Otherwise uploads and immediately
        deletes if not Ready (no grace period — we only care about hashes
        that are *already* cached, not ones AD can freshly download).

        :param hash_value: info hash, with or without the urn:btih: prefix
        :return: {hash_value: {"magnet_id": int, "files": [...]}} or {}
        """
        clean_hash = hash_value.replace("urn:btih:", "").strip()
        magnet = f"magnet:?xt=urn:btih:{clean_hash}"

        # Fast path: hash already on AD and Ready — no upload needed.
        try:
            existing = self.saved_magnets()
            existing_list = existing if isinstance(existing, list) else (
                existing.get("magnets", []) if isinstance(existing, dict) and isinstance(existing.get("magnets"), list) else []
            )
            for m in existing_list:
                if isinstance(m, dict) and m.get("hash", "").lower() == clean_hash.lower():
                    if int(m.get("statusCode", -1)) == 4:
                        return {
                            clean_hash: {
                                "magnet_id": m.get("id"),
                                "files": m.get("files") or [],
                            }
                        }
                    # Already on AD but not Ready — bail.
                    break
        except Exception:
            pass

        # Upload the magnet. AD may return ready=true (already cached) or
        # ready=false (needs to download from swarm). If ready=false, this
        # magnet will never become Ready in a reasonable time on AD's swarm
        # for our purposes (we don't want to wait minutes/hours), so reject.
        upload = self.post_json("magnet/upload", magnet=[magnet])
        if not upload or not upload.get("magnets"):
            return {}

        magnets = upload["magnets"]
        item = magnets[0] if isinstance(magnets, list) and magnets else magnets
        if not isinstance(item, dict):
            return {}

        magnet_id = item.get("id")
        if not magnet_id:
            return {}

        # Already cached on AD.
        if item.get("ready") is True:
            files = self.post_json("magnet/status", id=magnet_id)
            files_tree = files.get("magnets", {}).get("files") if isinstance(files, dict) else None
            return {
                clean_hash: {
                    "magnet_id": magnet_id,
                    "files": files_tree or [],
                }
            }

        # Not cached on AD — immediately delete the queued upload so we don't
        # pollute the user's queue with dead magnets.
        try:
            self.post_json("magnet/delete", id=magnet_id)
        except Exception:
            pass
        return {}

    def delete_magnet(self, magnet_id):
        return self.post_json("magnet/delete", id=magnet_id)

    def saved_links(self):
        return self.get_json("user/links")

    @staticmethod
    def is_service_enabled():
        return g.get_bool_setting(AD_ENABLED_KEY) and g.get_setting(AD_AUTH_KEY) is not None

    def get_account_status(self):
        user_info = self.get_user_info()
        if not isinstance(user_info, dict):
            return "unknown"

        premium = user_info.get("isPremium")
        premium_until = user_info.get("premiumUntil", 0)
        subscribed = user_info.get("isSubscribed")
        trial = user_info.get("isTrial")

        if premium and premium_until > time.time():
            return "premium"
        elif subscribed:
            return "subscribed"
        elif trial:
            return "trial"
        else:
            return "unknown"

import xbmc
import xbmcgui

from resources.lib.debrid.all_debrid import AllDebrid
from resources.lib.modules.exceptions import GeneralCachingFailure
from resources.lib.modules.globals import g
from resources.lib.modules.resolver.torrent_resolvers.base_resolver import (
    TorrentResolverBase,
)


_AD_TERMINAL_ERROR_CODES = {5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}


def _flatten_v4_files(nodes, parent_path=""):
    """Flatten AllDebrid v4.1 `files` tree into a list of flat file dicts.

    Each input node is one of:
      - folder: {"n": <name>, "e": [<node>, ...]}
      - file:   {"n": <name>, "s": <size>, "l": <link>}

    Output items:
      {"path": "Folder/sub/file.mkv", "size": <bytes>, "link": "<url>"}
    """
    out = []
    for node in nodes or []:
        name = node.get("n", "")
        path = f"{parent_path}/{name}" if parent_path else name
        if "e" in node:
            out.extend(_flatten_v4_files(node["e"], path))
        elif "l" in node:
            out.append({"path": path, "size": node.get("s", 0), "link": node["l"]})
    return out


class AllDebridResolver(TorrentResolverBase):
    """
    Resolver for All Debrid
    """

    _POLL_INTERVAL_SEC = 3
    _POLL_TIMEOUT_SEC = 30

    def __init__(self):
        super().__init__()
        self.debrid_module = AllDebrid()
        self._source_normalization = (
            ("size", "size", lambda k: (k / 1024) / 1024),
            ("path", ["release_title", "path"], None),
            ("id", "id", None),
            ("link", "link", None),
        )
        self.magnet_id = None

    def _fetch_source_files(self, torrent, item_information):
        g.log(f"AllDebrid: uploading magnet hash={torrent['hash'][:8]}...", "info")
        upload = self.debrid_module.upload_magnet(torrent["hash"])
        g.log(f"AllDebrid: upload response keys={list(upload.keys()) if isinstance(upload, dict) else type(upload).__name__}", "info")
        if not upload or not upload.get("magnets"):
            raise GeneralCachingFailure(f"AllDebrid upload returned no magnets: {upload}")

        magnets = upload["magnets"]
        magnet = magnets[0] if isinstance(magnets, list) and magnets else None
        if magnet is None and isinstance(magnets, dict):
            magnet = magnets
        if not magnet:
            raise GeneralCachingFailure(f"AllDebrid upload returned empty magnet: {magnets}")
        if "error" in magnet:
            raise GeneralCachingFailure(magnet["error"])
        self.magnet_id = magnet["id"]
        g.log(f"AllDebrid: magnet id={self.magnet_id}", "info")

        magnet_obj = self._wait_for_ready(self.magnet_id)
        files = magnet_obj.get("files") or []
        g.log(f"AllDebrid: files count={len(files)}", "info")
        if not files:
            raise GeneralCachingFailure("AllDebrid returned no files for magnet")

        return _flatten_v4_files(files)

    def _wait_for_ready(self, magnet_id):
        elapsed = 0
        last = None
        stuck_zero_seeders_polls = 0
        progress = xbmcgui.DialogProgress()
        progress.create(g.ADDON_NAME, "Caching torrent on AllDebrid…")
        cancelled = False
        try:
            while elapsed <= self._POLL_TIMEOUT_SEC:
                if progress.iscanceled():
                    cancelled = True
                    break

                try:
                    last = self.debrid_module.magnet_status(magnet_id)
                except Exception as e:
                    g.log(f"AllDebrid magnet_status raised {type(e).__name__}: {e}", "warning")
                    xbmc.sleep(self._POLL_INTERVAL_SEC * 1000)
                    elapsed += self._POLL_INTERVAL_SEC
                    progress.update(int(elapsed * 100 / self._POLL_TIMEOUT_SEC), "Caching torrent on AllDebrid…")
                    continue

                if not last or not isinstance(last, dict):
                    g.log(f"AllDebrid magnet_status returned falsy/non-dict: {type(last).__name__}", "warning")
                    xbmc.sleep(self._POLL_INTERVAL_SEC * 1000)
                    elapsed += self._POLL_INTERVAL_SEC
                    progress.update(int(elapsed * 100 / self._POLL_TIMEOUT_SEC), "Caching torrent on AllDebrid…")
                    continue

                status_code = int(last.get("statusCode", -1))
                status_label = last.get("status", "?")
                seeders = int(last.get("seeders", 0) or 0)
                download_speed = int(last.get("downloadSpeed", 0) or 0)
                g.log(f"AllDebrid: status={status_label} code={status_code} seeders={seeders} speed={download_speed} elapsed={elapsed}s", "info")

                if status_code == 4 or last.get("status") == "Ready":
                    progress.update(100, "Cached.")
                    return last
                if status_code in _AD_TERMINAL_ERROR_CODES:
                    progress.close()
                    self.debrid_module.delete_magnet(magnet_id)
                    raise GeneralCachingFailure(last)

                # Early bail: AD has been "Downloading" but 0 seeders and 0 speed for 4 consecutive polls
                # (>=12s). The torrent is dead on the swarm and will never cache. Don't waste the rest
                # of the timeout window.
                if status_code in (1, 2) and seeders == 0 and download_speed == 0:
                    stuck_zero_seeders_polls += 1
                else:
                    stuck_zero_seeders_polls = 0

                if stuck_zero_seeders_polls >= 4:
                    g.log(
                        f"AllDebrid: bailing early after {elapsed}s — magnet has 0 seeders and 0 "
                        f"download speed for {stuck_zero_seeders_polls} consecutive polls; will never cache",
                        "warning",
                    )
                    break

                progress.update(int(elapsed * 100 / self._POLL_TIMEOUT_SEC), f"AllDebrid: {status_label}")
                xbmc.sleep(self._POLL_INTERVAL_SEC * 1000)
                elapsed += self._POLL_INTERVAL_SEC
        finally:
            progress.close()

        try:
            self.debrid_module.delete_magnet(magnet_id)
        except Exception:
            pass
        if cancelled:
            raise GeneralCachingFailure({"status": "Cancelled", "statusCode": -1})
        raise GeneralCachingFailure(last or {"status": "Timeout", "statusCode": -1})

    def resolve_stream_url(self, file_info):
        """
        Convert provided source file into a link playable through debrid service
        :param file_info: Normalised information on source file
        :return: streamable link
        """
        return self.debrid_module.resolve_hoster(file_info["link"])

    def _do_post_processing(self, item_information, torrent, identified_file):
        if g.get_bool_setting("alldebrid.autodelete") or identified_file is None:
            self.debrid_module.delete_magnet(self.magnet_id)
from resources.lib.debrid.all_debrid import AllDebrid
from resources.lib.modules.exceptions import GeneralCachingFailure
from resources.lib.modules.globals import g
from resources.lib.modules.resolver.torrent_resolvers.base_resolver import (
    TorrentResolverBase,
)


def _translate_to_v4_objects(objects: list[dict]) -> list[dict]:
    """This function mocks the AllDebrid v4.0 API version return objects.

    A single dict in the output list will look like:
    {
        "link": <url>,
        "filename": <filename>,
        "size": <file_size>,
        "files": [
            {"n": <filename>}
        ]
    }

    This way, the consuming cleaner/utility functions in the resolver will be able to
    filter for video-based files and the whole pipeline should work.
    """
    all_files = list()
    for file in objects:
        folder_files = file["e"]
        for ffile in folder_files:
            name = ffile["n"]
            size = ffile["s"]
            link = ffile["l"]

            new_file = {
                "link": link,
                "filename": name,
                "size": size,
                "files": [{"n": name}]
            }

            all_files.append(new_file)
    return all_files


class AllDebridResolver(TorrentResolverBase):
    """
    Resolver for All Debrid
    """

    def __init__(self):
        super().__init__()
        self.debrid_module = AllDebrid()
        self._source_normalization = (
            ("size", "size", lambda k: (k / 1024) / 1024),
            ("filename", ["release_title", "path"], None),
            ("id", "id", None),
            ("link", "link", None),
        )
        self.magnet_id = None

    def _fetch_source_files(self, torrent, item_information):
        self.magnet_id = self.debrid_module.upload_magnet(torrent['hash'])["magnets"][0]["id"]
        status = self.debrid_module.magnet_status(self.magnet_id)["magnets"]
        if status["status"] != "Ready":
            self.debrid_module.delete_magnet(self.magnet_id)
            raise GeneralCachingFailure(status)

        # The key in "status" is now called "files" instead of "links" and has a
        # different nesting structure.
        files = status["files"]
        all_files = _translate_to_v4_objects(files)

        return all_files

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

import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from nupstudio import ns2db
from nupstudio.lookup import proposed_fields


class NS2DBTests(unittest.TestCase):
    def test_search_builds_id_and_name_queries(self):
        body = json.dumps({"status": "success", "count": 1, "data": [
            {"title": "Game", "titleid": "0123456789ABCDEF", "publisher": None}
        ]}).encode()
        with patch.object(ns2db, "_download", return_value=body) as download:
            self.assertEqual(ns2db.search("titleid", "0123456789abcdef")[0]["title"], "Game")
            query = parse_qs(urlsplit(download.call_args.args[0]).query)
            self.assertEqual(query, {"titleid": ["0123456789ABCDEF"]})
            ns2db.search("q", "Game & Fun")
            self.assertEqual(parse_qs(urlsplit(download.call_args.args[0]).query),
                             {"q": ["Game & Fun"]})

    def test_invalid_queries_do_not_request_network(self):
        with patch.object(ns2db, "_download") as download:
            for mode, query in (("titleid", "123"), ("titleid", "0" * 16),
                                ("q", ""), ("q", "a" * 1021), ("other", "x")):
                with self.subTest(mode=mode, query=query), self.assertRaises(ns2db.LookupError):
                    ns2db.search(mode, query)
            download.assert_not_called()

    def test_invalid_responses_are_reported(self):
        bodies = [
            b"not json", b'{"status":"error","count":0,"data":[]}',
            b'{"status":"success","count":1,"data":[]}',
            b'{"status":"success","count":true,"data":[]}',
            b'{"status":"success","count":1,"data":[{"title":4}]}',
        ]
        for body in bodies:
            with self.subTest(body=body), patch.object(ns2db, "_download", return_value=body):
                with self.assertRaises(ns2db.LookupError):
                    ns2db.search("q", "game")

    def test_transport_failure_and_response_size(self):
        for error in (URLError("offline"), HTTPError("https://example.com", 503, "", {}, None)):
            with self.subTest(error=error), patch.object(ns2db, "build_opener") as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(ns2db.LookupError):
                    ns2db._download("https://example.com", 3, same_host=True)
        with patch.object(ns2db, "build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value = b"abcd"
            with self.assertRaises(ns2db.LookupError):
                ns2db._download("https://example.com", 3, same_host=True)

    def test_cover_urls_and_redirects_require_https(self):
        for url in ("http://example.com/image", "https://user:pass@example.com/image",
                    "https://[invalid/image", "https://example.com:invalid/image",
                    "https://example.com/image path"):
            with self.subTest(url=url), self.assertRaises(ns2db.LookupError):
                ns2db.download_cover(url)
        redirect = ns2db._SafeRedirect("ns2db.ddnsfree.com")
        from urllib.request import Request
        request = Request(ns2db.API_URL)
        with self.assertRaises(ns2db.LookupError):
            redirect.redirect_request(request, None, 302, "", {}, "http://example.com")
        with self.assertRaises(ns2db.LookupError):
            redirect.redirect_request(request, None, 302, "", {}, "https://example.com")
        with patch.object(ns2db, "_download", return_value=b"image") as download:
            ns2db.download_cover("/covers/image.png")
            self.assertEqual(download.call_args.args[0],
                             "https://ns2db.ddnsfree.com/covers/image.png")

    def test_metadata_mapping_and_numeric_boundaries(self):
        release = {"title": "Game", "titleid": "0123456789abcdef",
                   "publisher": None, "base_version": "4294967295",
                   "update_version": "4294967296", "display_version": "1.2.3"}
        fields, notes = proposed_fields(release, {
            "title", "title_id", "publisher", "base_version", "update_version",
            "display_version",
        })
        self.assertEqual(fields, {"title": "Game", "title_id": "0123456789ABCDEF",
                                  "base_version": "4294967295", "display_version": "1.2.3"})
        self.assertTrue(any("Update version" in note for note in notes))
        self.assertTrue(any("whole number between 0 and 4,294,967,295" in note
                            for note in notes))
        for value in ("-1", "1.2", "4294967296"):
            self.assertFalse(ns2db.valid_version(value))
        for value in ("0", "0xFFFFFFFF", "4294967295"):
            self.assertTrue(ns2db.valid_version(value))
        fields, notes = proposed_fields(release, {"title"})
        self.assertEqual(fields, {"title": "Game"})
        self.assertTrue(any("Title ID" in note for note in notes))


if __name__ == "__main__":
    unittest.main()

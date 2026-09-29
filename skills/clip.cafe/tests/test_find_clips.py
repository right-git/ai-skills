import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "find-clips.py"
SPEC = importlib.util.spec_from_file_location("find_clips", SCRIPT)
find_clips = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(find_clips)


API_PAYLOAD = {
    "total": 1,
    "results": [
        {
            "clipID": 6285536,
            "slug": "oh-my-god-s183",
            "movie_slug": "spider-man-2002",
            "movie_title": "Spider Man",
            "movie_year": 2002,
            "title": "Oh, my God.",
            "duration": 11,
            "lineText": "Oh my God",
            "matchIn": "dialog",
            "hlsUrl": "https://clip.cafe/hls/x/master.m3u8",
            "mp4Url": "https://clip.cafe/videos/oh-my-god-s183.mp4",
            "audioLangs": ["en"],
            "subLangs": ["en", "ru"],
        }
    ],
}

SEARCH_HTML = """
<div class="searchResultClip">
  <a class="smallClipContainer" href="spider-man-2002/its-okay-s477/">
    <picture data-slug="its-okay-s477">
      <img data-src="https://clip.cafe/img800/its-okay-s477.jpg">
    </picture>
  </a>
  <div class="clipMovie"><a href="spider-man-2002">Spider Man</a> • 2002</div>
  <div class="clipTrans"><p><b>Transcript:</b><br>Oh, my God.</p></div>
</div>
"""

DETAIL_HTML = """
<picture class="clipThumb">
  <img src="https://clip.cafe/img400/unrelated-card.jpg">
</picture>
<div id="playerCont" onclick="startplayer('6285616')">
<picture>
  <img src="https://clip.cafe/img800/its-okay-s477.jpg">
</picture>
<video><source src="https://clip.cafe/videos/its-okay-s477.mp4"
 type="video/mp4"></video>
</div>
<div class="clip-meta-item"><div class="clip-meta-label">Duration</div>
<div class="clip-meta-value">28 seconds</div></div>
<div class="clip-meta-item"><div class="clip-meta-label">Timestamp in Movie</div>
<div class="clip-meta-value">01:23:07</div></div>
"""


class FakeResponse:
    def __init__(
        self,
        *,
        json_data=None,
        text="",
        status_code=200,
        headers=None,
        json_error=None,
    ):
        self._json_data = json_data
        self.text = text
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class FakeDownloadResponse:
    def __init__(
        self,
        body=b"video-bytes",
        content_type="video/mp4",
        on_iter=None,
        status_code=200,
    ):
        self.body = body
        self.headers = {"content-type": content_type}
        self.on_iter = on_iter
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_bytes(self):
        if self.on_iter:
            self.on_iter()
        yield self.body


class FakeStreamContext:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeDownloadClient:
    def __init__(
        self,
        body=b"video-bytes",
        content_type="video/mp4",
        on_iter=None,
        status_code=200,
    ):
        self.response = FakeDownloadResponse(
            body, content_type, on_iter, status_code
        )
        self.calls = 0
        self.stream_kwargs = None

    def stream(self, method, url, **kwargs):
        self.calls += 1
        self.stream_kwargs = kwargs
        return FakeStreamContext(self.response)


class ArgumentTests(unittest.TestCase):
    def test_quiet_download_mode_is_available(self):
        args = find_clips.build_parser().parse_args(
            ["line", "--download", "clip.mp4", "--quiet"]
        )

        self.assertTrue(args.quiet)

    def test_short_decade_maps_to_full_year_range(self):
        self.assertEqual(find_clips.parse_decade("80s"), (1980, 1989))

    def test_four_digit_decade_with_suffix_maps_to_full_year_range(self):
        self.assertEqual(find_clips.parse_decade("1980s"), (1980, 1989))

    def test_four_digit_decade_maps_to_full_year_range(self):
        self.assertEqual(find_clips.parse_decade("1980"), (1980, 1989))

    def test_non_decade_is_rejected(self):
        with self.assertRaises(ValueError):
            find_clips.parse_decade("1985")

    def test_requested_filters_use_observed_site_names(self):
        args = find_clips.build_parser().parse_args(
            [
                "oh my god",
                "--exact",
                "--title",
                "Spider Man",
                "--actor",
                "Tobey Maguire",
                "--character",
                "Peter Parker",
                "--director",
                "Sam Raimi",
                "--writer",
                "David Koepp",
                "--type",
                "movie",
                "--rating",
                "teen",
                "--genre",
                "action",
                "--language",
                "en",
                "--audio-language",
                "en",
                "--subtitle-language",
                "ru",
                "--decade",
                "80s",
                "--sort",
                "views",
                "--limit",
                "3",
            ]
        )
        params = find_clips.build_search_params(args)
        self.assertEqual(params["phrasefind"], '"oh my god"')
        self.assertEqual(params["title"], "Spider Man")
        self.assertEqual(params["actor"], "Tobey Maguire")
        self.assertEqual(params["character"], "Peter Parker")
        self.assertEqual(params["director"], "Sam Raimi")
        self.assertEqual(params["writer"], "David Koepp")
        self.assertEqual(params["type"], "movie")
        self.assertEqual(params["rated"], "teen")
        self.assertEqual(params["genre"], "action")
        self.assertEqual(params["lang"], "en")
        self.assertEqual(params["audio"], "en")
        self.assertEqual(params["subs"], "ru")
        self.assertEqual((params["yearStart"], params["yearEnd"]), (1980, 1989))
        self.assertEqual(params["sort"], "views")
        self.assertEqual(params["size"], 3)

    def test_decade_conflicts_with_explicit_years(self):
        args = find_clips.build_parser().parse_args(
            ["line", "--decade", "80s", "--year-start", "1982"]
        )
        with self.assertRaises(ValueError):
            find_clips.build_search_params(args)


class ParsingTests(unittest.TestCase):
    def test_api_result_is_normalized(self):
        result = find_clips.parse_api_payload(API_PAYLOAD)[1][0]
        self.assertEqual(result["clip_id"], 6285536)
        self.assertEqual(result["movie_title"], "Spider Man")
        self.assertEqual(result["displayed_quote"], "Oh, my God.")
        self.assertEqual(result["matched_line"], "Oh my God")
        self.assertEqual(
            result["page_url"],
            "https://clip.cafe/spider-man-2002/oh-my-god-s183/",
        )

    def test_html_cards_form_fallback_results(self):
        results = find_clips.parse_search_html(SEARCH_HTML)
        self.assertEqual(results[0]["movie_year"], 2002)
        self.assertEqual(results[0]["movie_title"], "Spider Man")
        self.assertEqual(results[0]["slug"], "its-okay-s477")
        self.assertEqual(results[0]["displayed_quote"], "Oh, my God.")
        self.assertEqual(
            results[0]["thumbnail_url"],
            "https://clip.cafe/img800/its-okay-s477.jpg",
        )

    def test_invalid_json_uses_html_fallback(self):
        client = FakeClient(
            [
                FakeResponse(json_error=ValueError("not json")),
                FakeResponse(
                    text=SEARCH_HTML,
                    headers={"content-type": "text/html; charset=UTF-8"},
                ),
            ]
        )
        params = {"phrasefind": "oh my god", "lang": "en", "from": 0, "size": 5}

        found = find_clips.search_clips(client, params)

        self.assertEqual(found["source"], "html")
        self.assertEqual(found["results"][0]["movie_title"], "Spider Man")
        self.assertEqual(len(client.calls), 2)
        fallback_params = client.calls[1][1]["params"]
        self.assertEqual(fallback_params["s"], "oh my god")
        self.assertEqual(fallback_params["ss"], "s")
        self.assertNotIn("phrasefind", fallback_params)

    def test_valid_empty_api_result_does_not_fall_back(self):
        client = FakeClient([FakeResponse(json_data={"total": 0, "results": []})])

        found = find_clips.search_clips(
            client,
            {"phrasefind": "never spoken", "lang": "en", "from": 0, "size": 5},
        )

        self.assertEqual(found, {"source": "api", "total": 0, "results": []})
        self.assertEqual(len(client.calls), 1)


class EnrichmentTests(unittest.TestCase):
    def test_download_enriches_only_the_selected_slug(self):
        results = [
            {"slug": "first"},
            {"slug": "selected"},
            {"slug": "third"},
        ]

        targets = find_clips.results_to_enrich(
            results,
            downloading=True,
            pick=1,
            slug="selected",
        )

        self.assertEqual(targets, [{"slug": "selected"}])

    def test_anchor_adds_clip_local_timing(self):
        result = find_clips.empty_result()

        find_clips.apply_anchor(
            result,
            {
                "ok": True,
                "startSec": 14.722,
                "endSec": 17.6,
                "lineText": "Oh my God",
                "exact": True,
            },
        )

        self.assertEqual(result["match_start_sec"], 14.722)
        self.assertEqual(result["match_end_sec"], 17.6)
        self.assertEqual(result["matched_line"], "Oh my God")
        self.assertTrue(result["exact"])

    def test_detail_page_adds_movie_timestamp_and_public_mp4(self):
        details = find_clips.parse_detail_html(DETAIL_HTML)

        self.assertEqual(details["clip_id"], 6285616)
        self.assertEqual(details["movie_timestamp"], "01:23:07")
        self.assertEqual(details["duration_sec"], 28)
        self.assertEqual(
            details["mp4_url"],
            "https://clip.cafe/videos/its-okay-s477.mp4",
        )
        self.assertEqual(
            details["thumbnail_url"],
            "https://clip.cafe/img800/its-okay-s477.jpg",
        )

    def test_detail_failure_does_not_discard_successful_anchor(self):
        result = find_clips.empty_result()
        result.update(
            {
                "clip_id": 6285616,
                "page_url": "https://clip.cafe/spider-man-2002/its-okay-s477/",
            }
        )
        client = FakeClient(
            [
                FakeResponse(status_code=500),
                FakeResponse(
                    json_data={
                        "ok": True,
                        "startSec": 14.722,
                        "endSec": 17.6,
                        "lineText": "Oh my God",
                        "exact": True,
                    }
                ),
            ]
        )

        find_clips.enrich_results(client, "oh my god", "en", [result])

        self.assertEqual(result["match_start_sec"], 14.722)
        self.assertIsNone(result["movie_timestamp"])
        self.assertEqual(len(result["enrichment_errors"]), 1)
        self.assertIn("detail", result["enrichment_errors"][0])


class DownloadTests(unittest.TestCase):
    def test_download_sends_page_referer_and_media_accept_header(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "clip.mp4"
            client = FakeDownloadClient()

            find_clips.download_clip(
                client,
                {
                    "mp4_url": "https://clip.cafe/videos/example.mp4",
                    "page_url": "https://clip.cafe/movie/example/",
                },
                destination,
                force=False,
            )

            self.assertEqual(
                client.stream_kwargs["headers"],
                {
                    "Referer": "https://clip.cafe/movie/example/",
                    "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
                },
            )

    def test_pick_is_one_based(self):
        self.assertEqual(
            find_clips.pick_result([{"slug": "a"}, {"slug": "b"}], 2),
            {"slug": "b"},
        )

    def test_pick_out_of_range_is_rejected(self):
        with self.assertRaises(ValueError):
            find_clips.pick_result([{"slug": "a"}], 2)

    def test_slug_selection_is_stable_when_results_reorder(self):
        reordered = [{"slug": "b"}, {"slug": "a"}]

        selected = find_clips.select_result(reordered, pick=2, slug="b")

        self.assertEqual(selected, {"slug": "b"})

    def test_unknown_slug_is_rejected(self):
        with self.assertRaises(ValueError):
            find_clips.select_result([{"slug": "a"}], pick=1, slug="missing")

    def test_existing_destination_requires_force_before_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "clip.mp4"
            destination.write_bytes(b"keep-me")
            client = FakeDownloadClient()

            with self.assertRaises(FileExistsError):
                find_clips.download_clip(
                    client,
                    {"mp4_url": "https://clip.cafe/videos/example.mp4"},
                    destination,
                    force=False,
                )

            self.assertEqual(client.calls, 0)
            self.assertEqual(destination.read_bytes(), b"keep-me")

    def test_forbidden_download_explains_authorized_download_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "clip.mp4"
            client = FakeDownloadClient(status_code=403)

            with self.assertRaisesRegex(
                find_clips.DownloadError,
                "authorized download flow",
            ):
                find_clips.download_clip(
                    client,
                    {
                        "mp4_url": "https://clip.cafe/videos/example.mp4",
                        "page_url": "https://clip.cafe/movie/example/",
                    },
                    destination,
                    force=False,
                )

            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_browser_fallback_is_used_only_when_explicitly_enabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "clip.mp4"
            client = FakeDownloadClient(status_code=403)
            calls = []

            def fallback(page_url, output, *, force, timeout):
                calls.append((page_url, output, force, timeout))
                output.write_bytes(b"browser-video")
                return output

            saved = find_clips.download_result(
                client,
                {
                    "mp4_url": "https://clip.cafe/videos/example.mp4",
                    "page_url": "https://clip.cafe/movie/example/",
                },
                destination,
                force=False,
                browser_fallback=True,
                capture_runner=fallback,
                timeout=12,
            )

            self.assertEqual(saved, destination)
            self.assertEqual(destination.read_bytes(), b"browser-video")
            self.assertEqual(
                calls,
                [
                    (
                        "https://clip.cafe/movie/example/",
                        destination,
                        False,
                        12,
                    )
                ],
            )

    def test_browser_fallback_failure_becomes_download_error(self):
        client = FakeDownloadClient(status_code=403)

        def fallback(_page_url, _output, *, force, timeout):
            raise RuntimeError("no media observed")

        with tempfile.TemporaryDirectory() as temp_dir, self.assertRaisesRegex(
            find_clips.DownloadError,
            "browser fallback failed: no media observed",
        ):
            find_clips.download_result(
                client,
                {
                    "mp4_url": "https://clip.cafe/videos/example.mp4",
                    "page_url": "https://clip.cafe/movie/example/",
                },
                Path(temp_dir) / "clip.mp4",
                force=False,
                browser_fallback=True,
                capture_runner=fallback,
                timeout=12,
            )

    def test_html_download_body_is_rejected_and_temp_is_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "clip.mp4"
            client = FakeDownloadClient(b"<html>error</html>", "text/html")

            with self.assertRaises(find_clips.DownloadError):
                find_clips.download_clip(
                    client,
                    {"mp4_url": "https://clip.cafe/videos/example.mp4"},
                    destination,
                    force=False,
                )

            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_late_destination_creation_is_not_overwritten_without_force(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "clip.mp4"
            client = FakeDownloadClient(
                on_iter=lambda: destination.write_bytes(b"created-by-other-process")
            )

            with self.assertRaises(FileExistsError):
                find_clips.download_clip(
                    client,
                    {"mp4_url": "https://clip.cafe/videos/example.mp4"},
                    destination,
                    force=False,
                )

            self.assertEqual(destination.read_bytes(), b"created-by-other-process")
            self.assertEqual(list(Path(temp_dir).iterdir()), [destination])

    def test_valid_video_is_installed_at_exact_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "nested" / "clip.mp4"
            client = FakeDownloadClient(b"video-bytes", "video/mp4")

            saved = find_clips.download_clip(
                client,
                {"mp4_url": "https://clip.cafe/videos/example.mp4"},
                destination,
                force=False,
            )

            self.assertEqual(saved, destination)
            self.assertEqual(destination.read_bytes(), b"video-bytes")


class OutputTests(unittest.TestCase):
    def test_cli_help_runs_after_all_functions_are_defined(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--download", completed.stdout)

    def test_human_output_labels_both_timeline_types(self):
        result = find_clips.empty_result()
        result.update(
            {
                "movie_title": "Spider Man",
                "movie_year": 2002,
                "matched_line": "Oh my God",
                "match_start_sec": 1,
                "match_end_sec": 2.34,
                "movie_timestamp": "01:23:07",
                "source_match_start": "01:23:08.000",
                "source_match_end": "01:23:09.340",
                "page_url": "https://clip.cafe/spider-man-2002/example/",
                "mp4_url": "https://clip.cafe/videos/example.mp4",
            }
        )

        output = find_clips.format_human([result])

        self.assertIn("1. Spider Man (2002)", output)
        self.assertIn("clip time: 1–2.34 seconds", output)
        self.assertIn("clip starts in source: 01:23:07", output)
        self.assertIn(
            "phrase in source: 01:23:08.000–01:23:09.340",
            output,
        )

    def test_source_phrase_times_are_calculated_from_clip_start(self):
        result = find_clips.empty_result()
        result["movie_timestamp"] = "00:17:16"

        find_clips.apply_anchor(
            result,
            {
                "ok": True,
                "startSec": 1,
                "endSec": 2.34,
                "lineText": "Oh my God",
                "exact": True,
            },
        )

        self.assertEqual(result["source_match_start"], "00:17:17.000")
        self.assertEqual(result["source_match_end"], "00:17:18.340")

    def test_output_document_preserves_query_filters_and_source(self):
        args = find_clips.build_parser().parse_args(
            ["oh my god", "--exact", "--title", "Spider Man"]
        )

        document = find_clips.build_output_document(
            args,
            {"source": "api", "total": 1, "results": []},
        )

        self.assertEqual(document["query"], "oh my god")
        self.assertEqual(document["filters"]["exact"], True)
        self.assertEqual(document["filters"]["title"], "Spider Man")
        self.assertEqual(document["source"], "api")


if __name__ == "__main__":
    unittest.main()

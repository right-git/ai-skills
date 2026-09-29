import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "capture-media.py"
SPEC = importlib.util.spec_from_file_location("capture_media", SCRIPT)
capture_media = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(capture_media)


class ClassificationTests(unittest.TestCase):
    def test_recognizes_mp4_from_url_or_content_type(self):
        self.assertEqual(
            capture_media.classify_media("https://clip.cafe/video/example.mp4", ""),
            "mp4",
        )
        self.assertEqual(
            capture_media.classify_media(
                "https://cdn.example/media?id=1", "video/mp4; charset=binary"
            ),
            "mp4",
        )

    def test_recognizes_hls_from_url_or_content_type(self):
        self.assertEqual(
            capture_media.classify_media(
                "https://clip.cafe/hls/example/master.m3u8", ""
            ),
            "hls",
        )
        self.assertEqual(
            capture_media.classify_media(
                "https://cdn.example/manifest", "application/vnd.apple.mpegurl"
            ),
            "hls",
        )

    def test_rejects_non_https_media(self):
        self.assertIsNone(
            capture_media.classify_media("http://clip.cafe/video/example.mp4", "")
        )


class HeaderTests(unittest.TestCase):
    def test_copies_only_safe_headers_and_drops_partial_range(self):
        headers = capture_media.safe_request_headers(
            {
                "User-Agent": "Browser UA",
                "Referer": "https://clip.cafe/movie/clip/",
                "Accept": "*/*",
                "Cookie": "private=1",
                "Authorization": "Bearer private",
                "Range": "bytes=0-1",
            },
            "https://clip.cafe/movie/clip/",
        )

        self.assertEqual(
            headers,
            {
                "User-Agent": "Browser UA",
                "Referer": "https://clip.cafe/movie/clip/",
                "Accept": "*/*",
            },
        )


class CandidateTests(unittest.TestCase):
    def test_prefers_mp4_and_deduplicates_repeated_range_responses(self):
        selected = capture_media.choose_candidate(
            [
                {"url": "https://clip.cafe/hls/x/master.m3u8", "kind": "hls"},
                {"url": "https://clip.cafe/videos/x.mp4", "kind": "mp4"},
                {"url": "https://clip.cafe/videos/x.mp4", "kind": "mp4"},
            ]
        )

        self.assertEqual(selected["url"], "https://clip.cafe/videos/x.mp4")

    def test_clip_page_must_be_public_https_clip_cafe_url(self):
        self.assertEqual(
            capture_media.validate_page_url("https://clip.cafe/movie/clip/"),
            "https://clip.cafe/movie/clip/",
        )
        for invalid in (
            "http://clip.cafe/movie/clip/",
            "https://example.com/movie/clip/",
            "https://user:pass@clip.cafe/movie/clip/",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                capture_media.validate_page_url(invalid)

    def test_collector_marks_successful_mp4_response_ready_immediately(self):
        class FakeRequest:
            method = "GET"
            resource_type = "media"

            @staticmethod
            def all_headers():
                return {
                    "user-agent": "Browser UA",
                    "referer": "https://clip.cafe/movie/clip/",
                    "range": "bytes=0-",
                }

        class FakeResponse:
            url = "https://clip.cafe/videos/example.mp4"
            status = 206
            request = FakeRequest()

            @staticmethod
            def all_headers():
                return {"content-type": "video/mp4"}

        collector = capture_media.MediaCollector(
            "https://clip.cafe/movie/clip/"
        )

        collector.observe(FakeResponse())

        self.assertTrue(collector.mp4_ready)
        self.assertEqual(collector.candidates[0]["kind"], "mp4")
        self.assertNotIn("Range", collector.candidates[0]["headers"])


class DownloadTests(unittest.TestCase):
    def test_existing_destination_is_refused_without_force(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "clip.mp4"
            destination.write_bytes(b"keep")

            with self.assertRaises(FileExistsError):
                capture_media.prepare_destination(destination, force=False)

            self.assertEqual(destination.read_bytes(), b"keep")

    def test_ffmpeg_headers_use_crlf_without_sensitive_values(self):
        value = capture_media.format_ffmpeg_headers(
            {
                "User-Agent": "Browser UA",
                "Referer": "https://clip.cafe/movie/clip/",
            }
        )

        self.assertEqual(
            value,
            "User-Agent: Browser UA\r\nReferer: https://clip.cafe/movie/clip/\r\n",
        )


if __name__ == "__main__":
    unittest.main()

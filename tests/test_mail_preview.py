import unittest
from mailbot.mail_preview import MAX_MESSAGE_BYTES, preview


class MailPreviewTests(unittest.TestCase):
    def test_plain_is_bounded_and_honest(self):
        result = preview(b'Subject: Demo\nFrom: demo@example.invalid\n\nhello world', 5)
        self.assertEqual(result['preview'], 'hello')
        self.assertTrue(result['truncated'])
        self.assertEqual(result['llm'], 'NOT_CONFIGURED')

    def test_html_drops_script_and_decodes_entities(self):
        result = preview(b'Content-Type: text/html; charset=utf-8\n\n<p>A &amp; B</p><script>secret</script><style>x</style>')
        self.assertEqual(result['preview'], 'A & B')

    def test_attachment_is_not_body(self):
        raw = b'MIME-Version: 1.0\nContent-Type: multipart/mixed; boundary=x\n\n--x\nContent-Type: text/plain\n\nbody\n--x\nContent-Type: text/plain\nContent-Disposition: attachment; filename=secret.txt\n\nprivate attachment\n--x--\n'
        self.assertEqual(preview(raw)['preview'], 'body')

    def test_limits(self):
        with self.assertRaises(ValueError):
            preview(b'x' * (MAX_MESSAGE_BYTES + 1))
        with self.assertRaises(ValueError):
            preview(b'', 0)

import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from mailbot.config import Settings
from mailbot.gmail_notifier import GmailNotifier, notification_text


class Engine:
    def __init__(self): self.sent = []; self.polled = False
    def poll_once(self, client): self.polled = True; return {'state':'READY'}
    def status(self): return {'state':'READY'}
    def pending(self, limit): return [{'message_id':'abc123','sender':'sender','subject':'test','snippet':'text'}]
    def mark_sent(self, message_id, sent_id): self.sent.append((message_id, sent_id))


class Channel:
    id = 2
    guild = SimpleNamespace(id=1)
    def __init__(self, fail=False): self.fail=fail; self.posts=[]
    async def send(self, text, **kwargs):
        if self.fail: raise RuntimeError('synthetic failure')
        self.posts.append(text)
        return SimpleNamespace(id=99)


class GmailNotifierTests(unittest.TestCase):
    def notifier(self):
        n=GmailNotifier(Settings(allowed_guild_ids=frozenset({1}),allowed_channel_ids=frozenset({2}),allowed_user_ids=frozenset({3})))
        n.engine=Engine(); n.client=object()
        return n

    def test_disabled_does_not_open_credentials(self):
        n=GmailNotifier(Settings(gmail_token_path=Path('/unavailable')))
        self.assertFalse(n.initialize())
        self.assertEqual(n.status()['state'],'NOT_CONFIGURED')

    def test_text_is_bounded_and_mentions_escaped(self):
        text=notification_text({'message_id':'a1','sender':'<@everyone>','subject':'*x*','snippet':'x'*10000})
        self.assertLess(len(text),1900)
        self.assertNotIn('<@',text)
        self.assertIn('mail.google.com',text)

    def test_marks_sent_only_after_delivery(self):
        n=self.notifier(); channel=Channel()
        bot=SimpleNamespace(get_channel=lambda _:channel)
        discord=SimpleNamespace(AllowedMentions=SimpleNamespace(none=lambda:None))
        asyncio.run(n.cycle(bot,discord))
        self.assertEqual(n.engine.sent,[('abc123','99')])
        self.assertEqual(len(channel.posts),1)

    def test_failure_preserves_pending_and_wrong_channel_never_polls(self):
        n=self.notifier(); channel=Channel(fail=True)
        bot=SimpleNamespace(get_channel=lambda _:channel)
        discord=SimpleNamespace(AllowedMentions=SimpleNamespace(none=lambda:None))
        with self.assertRaises(RuntimeError): asyncio.run(n.cycle(bot,discord))
        self.assertEqual(n.engine.sent,[])
        channel.guild=SimpleNamespace(id=999)
        n.engine.polled=False
        self.assertFalse(asyncio.run(n.cycle(bot,discord)))
        self.assertFalse(n.engine.polled)

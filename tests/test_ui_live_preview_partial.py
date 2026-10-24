"""Partial album acknowledgements must not displace a complete preview."""
import json

from poster_bot.ui import texts
from test_ui_live_preview import live_harness
from test_ui_preview_lifecycle import SizedVisibleApi
from test_ui_handlers_offline import OWNER


class PartialAlbumApi(SizedVisibleApi):
    async def make_request(self, bot, method, timeout=None):
        if method.__api_method__ != 'sendMediaGroup':
            return await super().make_request(bot, method, timeout)
        payload = method.model_dump(mode='json', exclude_none=True,
                                    exclude={'default'}, fallback=str)
        assert payload['chat_id'] == OWNER
        self.requests.append(('sendMediaGroup', payload))
        raw = self._raw()
        self.visible[raw['message_id']] = payload
        self.roles[raw['message_id']] = 'preview'
        return self.check_response(bot=bot, method=method, status_code=200,
            content=json.dumps({'ok': True, 'result': [raw]})).result


def test_partial_album_acknowledgements_remain_known_without_promotion(services):
    h = live_harness(services)
    h.message(message_id=40, photo=[{
        'file_id': 'first', 'file_unique_id': 'first', 'width': 100, 'height': 100}])
    old = h.session.alive('preview')
    h.session.__class__ = PartialAlbumApi
    h.message(message_id=41, photo=[{
        'file_id': 'second', 'file_unique_id': 'second', 'width': 100, 'height': 100}])
    assert old.issubset(h.session.visible)
    data = h.flow._navigation.lifecycle._load()
    assert set(data['active']) == old
    known = set(data['active'] + data['candidate']) | {int(mid) for mid in data['pending']}
    assert h.session.alive('preview').issubset(known)
    assert h.flow.input_captured(f'{OWNER}:41')
    assert not services.conn.execute('SELECT * FROM posts').fetchall()
    h.session.__class__ = SizedVisibleApi
    h.press_text('Повторить предпросмотр')
    assert len(h.session.alive('preview')) == 2
    assert not old.intersection(h.session.visible)

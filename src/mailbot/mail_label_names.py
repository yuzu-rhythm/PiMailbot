"""Display names and compatibility for processing labels and saved undo maps."""
NAMES = ('要対応', 'あとで読む', '保管', '不要', '処理済み', '見直し対象')
LEGACY = {name: 'Mailbot/' + name for name in NAMES}


def compatible_ids(ids):
    result = dict(ids)
    for name, old in LEGACY.items():
        if old in result:
            if name in result and result[name] != result[old]:
                raise ValueError('PROCESSING_LABEL_CONFLICT')
            result[name] = result[old]
    return result

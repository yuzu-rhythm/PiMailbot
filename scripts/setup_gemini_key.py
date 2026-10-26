#!/usr/bin/env python3
"""Save a key from a private terminal prompt; do not enable API use."""
import argparse
import getpass
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mailbot.api_credentials import valid_gemini_key


ERROR_HINTS = {
    'Unsafe credential path': '保存先が安全な通常のディレクトリではありません。',
    'Credential path must be ignored and untracked': '保存先がGitの除外対象・追跡対象外になっていません。',
    'Key already exists; use --replace if intended': '保存済みのキーがあります。上書きする場合だけ --replace を指定してください。',
    'Empty key': 'キーが入力されていません。コピーしたキー本体を貼り付けてEnterを押してください。',
    'Invalid key format': 'キーに空白・改行・非ASCII文字が含まれるか、長さが範囲外です。キー本体だけをコピーしてください。',
    'Unsafe credential directory': '保存先のディレクトリを安全に確認できません。',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print('Run this command in your own interactive terminal.', file=sys.stderr)
        return 2
    target = ROOT / 'data/gemini/api-key.txt'
    temporary = None
    try:
        # Refuse links, tracked files and destinations outside the ignored tree.
        for p in (target.parent.parent, target.parent, target):
            if p.is_symlink():
                raise ValueError('Unsafe credential path')
        for arguments, expected in ((['check-ignore', '--quiet', '--', 'data/gemini/api-key.txt'], 0),
                                    (['ls-files', '--error-unmatch', '--', 'data/gemini/api-key.txt'], 1)):
            result = subprocess.run(['git', *arguments], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode != expected:
                raise ValueError('Credential path must be ignored and untracked')
        if target.exists() and (not target.is_file() or not args.replace):
            raise ValueError('Key already exists; use --replace if intended')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            key = getpass.getpass('Gemini API key (hidden): ').strip()
        if not key:
            raise ValueError('Empty key')
        if not valid_gemini_key(key):
            raise ValueError('Invalid key format')
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(target.parent.lstat().st_mode):
            raise ValueError('Unsafe credential directory')
        target.parent.chmod(0o700)
        fd, name = tempfile.mkstemp(dir=target.parent, prefix='.key-')
        temporary = Path(name)
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(key + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        if target.is_symlink():
            raise ValueError('Unsafe credential path')
        os.replace(temporary, target)
        temporary = None
    except ValueError as error:
        print(ERROR_HINTS.get(str(error), '保存内容の安全チェックに失敗しました。'), file=sys.stderr)
        print('キーは保存していません。APIへの送信も行っていません。', file=sys.stderr)
        return 2
    except OSError:
        print('保存先への書き込みに失敗しました。空き容量・アクセス権を確認してください。APIへの送信は行っていません。', file=sys.stderr)
        return 2
    except getpass.GetPassWarning:
        print('入力を非表示にできません。Macの通常のターミナルで実行してください。APIへの送信は行っていません。', file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        return 130
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print('Key saved privately. Gemini remains disabled until separately configured.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

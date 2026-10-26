#!/usr/bin/env python3
"""Save a key from a private terminal prompt; do not enable API use."""
import argparse
import getpass
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import warnings

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print('Run this command in your own interactive terminal.', file=sys.stderr)
        return 2
    target = ROOT / 'data/todoist/token.txt'
    temporary = None
    try:
        # Refuse links, tracked files and destinations outside the ignored tree.
        for p in (target.parent.parent, target.parent, target):
            if p.is_symlink():
                raise ValueError('Unsafe credential path')
        for arguments, expected in ((['check-ignore', '--quiet', '--', 'data/todoist/token.txt'], 0),
                                    (['ls-files', '--error-unmatch', '--', 'data/todoist/token.txt'], 1)):
            result = subprocess.run(['git', *arguments], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode != expected:
                raise ValueError('Credential path must be ignored and untracked')
        if target.exists() and (not target.is_file() or not args.replace):
            raise ValueError('Key already exists; use --replace if intended')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            key = getpass.getpass('Todoist API token (hidden): ').strip()
        if not re.fullmatch(r'[A-Za-z0-9_.~-]{20,512}', key):
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
    except (OSError, ValueError, getpass.GetPassWarning):
        print('Could not store the key safely. No API request was made.', file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        return 130
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print('Key saved privately. Todoist remains disabled until separately configured.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

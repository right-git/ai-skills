"""TMDB poster finder: resolve exact film/TV record, list voted posters, download one.

Conventions follow clip.cafe/find-clips.py: --json review first, stable
--file-path bookmark for download, --quiet for reviewed downloads,
--force required to overwrite, atomic install, verified media bytes.

Auth: TMDB_API_KEY in repo-root .env (v4 read-access Bearer token).
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

API = 'https://api.themoviedb.org/3'
IMG = 'https://image.tmdb.org/t/p'
UA = {'User-Agent': 'video-gen-tmdb/1.0'}


def fail(msg, code=2):
    print(json.dumps({'status': 'error', 'error': msg})) if False else None
    print(f'tmdb: error: {msg}', file=sys.stderr)
    sys.exit(code)


def token():
    for path in ('.env', '../.env', '../../.env'):
        if os.path.isfile(path):
            for line in open(path, encoding='utf-8'):
                line = line.strip()
                if line.startswith('TMDB_API_KEY='):
                    val = line.split('=', 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
    if os.environ.get('TMDB_API_KEY'):
        return os.environ['TMDB_API_KEY']
    fail('BLOCKED: TMDB_API_KEY missing. Put the free v4 read-access token '
         'in the repo-root .env (see the tmdb skill).')


def api(path, params=None):
    url = API + path + '?' + urllib.parse.urlencode(params or {})
    req = urllib.request.Request(url, headers={**UA, 'Authorization': f'Bearer {token()}'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read(400).decode('utf-8', 'replace')
        fail(f'TMDB {e.code} on {path}: {body}')


def pick_media(args):
    if args.tv:
        data = api('/search/tv', {'query': args.tv, 'language': args.lang,
                                  'include_adult': 'false'})
        results = data.get('results', [])
        if not results:
            fail(f'no TV match for {args.tv!r}')
        items = [{'tmdb_id': r['id'], 'title': r.get('name'),
                  'original_title': r.get('original_name'),
                  'year': (r.get('first_air_date') or '')[:4] or None,
                  'overview': r.get('overview'),
                  'page_url': f"https://www.themoviedb.org/tv/{r['id']}"}
                 for r in results]
        kind = 'tv'
    else:
        if not args.title or not args.year:
            fail('--title and --year are both required for movies')
        data = api('/search/movie', {'query': args.title, 'year': args.year,
                                     'language': args.lang,
                                     'include_adult': 'false'})
        results = [r for r in data.get('results', [])
                   if (r.get('release_date') or '')[:4] == str(args.year)]
        if not results:
            same = [r for r in data.get('results', [])]
            hint = '; other years: ' + ', '.join(
                sorted({(r.get('release_date') or '?')[:4] + ' ' + (r.get('title') or '?')
                        for r in same})) if same else ''
            fail(f'no movie match for {args.title!r} ({args.year}){hint}')
        items = [{'tmdb_id': r['id'], 'title': r.get('title'),
                  'original_title': r.get('original_title'),
                  'year': (r.get('release_date') or '')[:4],
                  'overview': r.get('overview'),
                  'page_url': f"https://www.themoviedb.org/movie/{r['id']}"}
                 for r in results]
        kind = 'movie'
    if args.pick:
        if not 1 <= args.pick <= len(items):
            fail(f'--pick {args.pick} out of range (1..{len(items)})')
        return items[args.pick - 1], kind
    exact = [m for m in items
             if (m['title'] or '').lower() == (args.tv or args.title).lower()]
    if len(items) > 1 and not exact:
        fail(f"ambiguous: {len(items)} records; re-run with --pick N. " +
             '; '.join(f"{i + 1}={m['title']} ({m['year']}) [{m['tmdb_id']}]"
                       for i, m in enumerate(items[:8])))
    return (exact[0] if exact else items[0]), kind


def posters(media, kind, args):
    data = api(f'/{kind}/{media["tmdb_id"]}/images', {'language': args.lang})
    cands = []
    for p in data.get('posters', []):
        if args.min_width and (p.get('width') or 0) < args.min_width:
            continue
        if args.no_textless and p.get('iso_639_1') is None:
            continue
        cands.append({
            'file_path': p['file_path'],
            'width': p['width'], 'height': p['height'],
            'lang': p.get('iso_639_1'), 'vote_average': p.get('vote_average'),
            'vote_count': p.get('vote_count'),
            'preview_url': f"{IMG}/w342{p['file_path']}",
        })
    cands.sort(key=lambda c: (c['vote_count'] or 0, c['vote_average'] or 0),
               reverse=True)
    return cands[:args.limit]


def download(file_path, dest, args):
    if os.path.exists(dest) and not args.force:
        fail(f'refuses to overwrite {dest} (use --force to replace it)')
    size = 'original' if args.size == 'original' else args.size
    url = f'{IMG}/{size}{file_path}'
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            blob = r.read()
    except urllib.error.HTTPError as e:
        fail(f'image CDN {e.code} for {file_path}')
    if len(blob) < 20_000:
        fail(f'image too small ({len(blob)} B), likely an error page')
    if blob[:2] != b'\xff\xd8' and blob[:8] != b'\x89PNG\r\n\x1a\n':
        fail('not JPEG/PNG data (magic bytes mismatch)')
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = dest + '.part'
    with open(tmp, 'wb') as f:
        f.write(blob)
    os.replace(tmp, dest)
    return {'file': dest, 'bytes': len(blob), 'size': size}


def main():
    ap = argparse.ArgumentParser(description='TMDB official poster finder')
    ap.add_argument('--title', help='movie title (exact)')
    ap.add_argument('--year', help='movie release year (required with --title)')
    ap.add_argument('--tv', help='TV show title (instead of --title/--year)')
    ap.add_argument('--lang', default='ru-RU')
    ap.add_argument('--limit', type=int, default=8)
    ap.add_argument('--min-width', type=int, default=500)
    ap.add_argument('--no-textless', action='store_true')
    ap.add_argument('--pick', type=int)
    ap.add_argument('--file-path', help='stable TMDB file path from a review')
    ap.add_argument('--size', default='original',
                    help='original|w500|w780 (CDN rendition)')
    ap.add_argument('--download', help='destination path (nothing saved w/o it)')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    if args.file_path and (args.title or args.tv):
        media, kind = pick_media(args)
    elif args.file_path:
        media, kind = {'tmdb_id': None, 'title': None}, 'movie'
    else:
        media, kind = pick_media(args)

    if args.download:
        fp = args.file_path
        if not fp:
            cands = posters(media, kind, args)
            if not cands:
                fail('no poster candidates after filters')
            fp = cands[0]['file_path'] if not args.pick else \
                cands[min(args.pick, len(cands)) - 1]['file_path']
        saved = download(fp, args.download, args)
        if args.json or args.quiet:
            print(json.dumps({'status': 'saved', 'media': media,
                              'file_path': fp, **saved},
                             ensure_ascii=False, indent=1))
        else:
            print(f"saved: {saved['file']} ({saved['bytes']} B)")
        return

    cands = posters(media, kind, args)
    doc = {'query': {'title': args.title, 'year': args.year, 'tv': args.tv},
           'media': media, 'poster_count': len(cands), 'posters': cands}
    if args.json or not args.quiet:
        print(json.dumps(doc, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()

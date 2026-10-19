"""Optional illustrations bound to descriptive images in an article's cited sources."""
from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser
from urllib.parse import urljoin

from .config import ROOT


class ImageParser(HTMLParser):
    def __init__(self):
        super().__init__();self.images=[];self.excluded=0

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in {'nav','header','footer','aside'}:self.excluded+=1
        if tag=='img' and not self.excluded:
            self.images.append({'url':attrs.get('data-src') or attrs.get('src',''),
                                'description':attrs.get('alt') or ''})

    def handle_endtag(self,tag):
        if tag in {'nav','header','footer','aside'} and self.excluded:self.excluded-=1


def source_images(content: str, kind: str, source: str) -> list[dict]:
    from .research import source_url
    if kind=='html':
        parser=ImageParser();parser.feed(content);images=parser.images
    else:
        images=[{'description':m[0],'url':m[1]} for m in re.findall(r'!\[([^\]\n]+)\]\((https://[^\s)]+)\)',content)]
    result=[];seen=set()
    for image in images:
        description=' '.join(image['description'].split())
        # No guessed associations based only on the URL or generic Open Graph/site artwork.
        if not 12<=len(description)<=400 or re.search(r'logo|avatar|banner|icon|placeholder|advert|二维码|头像|广告|示意占位',description,re.I):continue
        try:url=source_url(urljoin(source,image['url'] or ''))
        except ValueError:continue
        if url in seen or any(c in url for c in '<>"\n\r'):continue
        seen.add(url);result.append({'url':url,'description':description})
        if len(result)>=4:break
    return result


def verify_image(url: str) -> dict:
    result=subprocess.run(['node',str(ROOT/'scripts/image.mjs')],input=json.dumps({'url':url}),
                          capture_output=True,text=True,check=True,timeout=25)
    return json.loads(result.stdout)


def illustrate(doc: dict, sources: list[dict], client) -> None:
    """Image failure must never discard an otherwise verified news article."""
    articles={a['event_key']:a for s in doc['sections'] for a in s['articles']}
    by_id={s['id']:s for s in sources};options={};requests=[]
    for key,article in articles.items():
        article.pop('image',None)
        cited={e['source_id'] for f in article['facts'] for e in f['evidence']}
        images=[]
        for sid in sorted(cited):
            source=by_id[sid]
            for image in source.get('images',[]):
                image_id='i'+str(len(options)+1)
                options[image_id]={**image,'source_id':sid,'source_url':source['url'],'event_key':key}
                images.append({'image_id':image_id,'description':image['description']})
        if images:requests.append({'event_key':key,'title':article['title'],'entities':article.get('entities',[]),
                                   'text':'\n'.join(f['text'] for f in article['facts']),'images':images})
    if not requests:return
    try:selection=client.request('illustrate',{'articles':requests}).get('images',[])
    except Exception:return
    if not isinstance(selection,list):return
    used=set()
    for item in selection:
        if not isinstance(item,dict):continue
        image_id=item.get('image_id');key=item.get('event_key');caption=item.get('caption')
        if not isinstance(image_id,str) or not isinstance(key,str):continue
        image=options.get(image_id)
        if (not image or key!=image['event_key'] or key in used or item.get('relevant') is not True
            or not isinstance(caption,str) or not 4<=len(caption)<=250):continue
        try:
            checked=verify_image(image['url'])
            # Preserve the exact publisher-embedded URL; redirects were independently checked.
            articles[key]['image']={'url':image['url'],'caption':caption,'source_id':image['source_id'],
                                    'source_url':image['source_url'],'width':checked['width'],'height':checked['height']}
            used.add(key)
        except (ValueError,KeyError,subprocess.SubprocessError):continue

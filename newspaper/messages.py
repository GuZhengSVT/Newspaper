"""Reader-facing news bodies shared by email and Telegram notifications."""
from __future__ import annotations

from html import escape

from .reading import report_view


def utf16_length(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def text_chunks(text: str, limit: int = 3500) -> list[str]:
    """Split before formatting, preserving every character, including astral emoji."""
    chunks=[]
    while text:
        end=0;size=0
        for char in text:
            size += 2 if ord(char)>0xffff else 1
            if size>limit:break
            end += 1
        if end<len(text):
            boundary=max(text.rfind('\n',0,end),text.rfind('。',0,end),text.rfind(' ',0,end))
            if boundary>end//2:end=boundary+1
        chunks.append(text[:end]);text=text[end:]
    return chunks


def news_message(settings, documents: list[tuple[str,dict]], subject: str) -> dict:
    plain=[];html=['<h1 style="margin:0 0 24px;font-family:Georgia,&#34;Times New Roman&#34;,&#34;Songti SC&#34;,serif;font-size:28px;line-height:1.4;font-weight:700">'+escape(subject)+'</h1>'];parts=[];pending=[];pending_size=0;links=[]

    def flush():
        nonlocal pending_size
        if pending:
            parts.append({'method':'sendMessage','text':'\n\n'.join(pending),'parse_mode':'HTML'})
            pending.clear();pending_size=0

    def block(text, tag=''):
        nonlocal pending_size
        plain.append(text)
        for chunk in text_chunks(text):
            if pending_size+utf16_length(chunk)+2>3800:flush()
            rendered=escape(chunk)
            pending.append(f'<{tag}>{rendered}</{tag}>' if tag else rendered)
            pending_size += utf16_length(chunk)+2

    block(subject,'b')
    for key,doc in documents:
        view=report_view(doc,key,settings)
        block(doc['title'],'b');html.append('<h2>'+escape(doc['title'])+'</h2>')
        if view['window']:
            window='报道时段：'+view['window'];block(window);html.append('<p>'+escape(window)+'</p>')
        if not view['count']:
            block('本期未收录新闻。');html.append('<p>本期未收录新闻。</p>')
        for section in view['sections']:
            block(section['name'],'b');html.append('<h3>'+escape(section['name'])+'</h3>')
            for article in section['articles']:
                block(article['title'],'b');html.append('<h4>'+escape(article['title'])+'</h4>')
                meta=article['time']
                if article.get('importance'):meta+=' · '+article['importance']+' · 可靠性 '+article['reliability']
                block(meta);html.append('<p style="color:#666;font-size:13px">'+escape(meta)+'</p>')
                for paragraph in article['paragraphs']:
                    block(paragraph);html.append('<p>'+escape(paragraph)+'</p>')
                image=article.get('image')
                if image:
                    flush()
                    caption=article['title']+'\n'+image['caption']
                    # Caption is explanatory only; full news text is always sent separately.
                    parts.append({'method':'sendPhoto','photo':image['url'],
                                  'caption':escape(text_chunks(caption,900)[0]),'parse_mode':'HTML'})
                    html.append('<figure style="margin:16px 0"><img src="'+escape(image['url'],quote=True)+
                        '" alt="'+escape(image['caption'],quote=True)+'" width="'+str(min(image['width'],680))+
                        '" style="display:block;max-width:100%;height:auto"><figcaption>'+escape(image['caption'])+
                        ' · <a href="'+escape(image['source_url'],quote=True)+'">图片来源</a></figcaption></figure>')
                if article['references']:
                    refs=article['references']
                    plain.append('来源：'+' · '.join(r['label']+' '+r['url'] for r in refs))
                    rendered='来源：'+' · '.join('<a href="'+escape(r['url'],quote=True)+'">'+escape(r['label'])+'</a>' for r in refs)
                    group=[];length=3
                    for ref in refs:
                        label=ref['label'];link='<a href="'+escape(ref['url'],quote=True)+'">'+escape(label)+'</a>'
                        if group and length+utf16_length(label)+3>3500:
                            if pending_size+length+2>3800:flush()
                            pending.append('来源：'+' · '.join(group));pending_size+=length+2
                            group=[];length=3
                        group.append(link);length+=utf16_length(label)+3
                    if group:
                        if pending_size+length+2>3800:flush()
                        pending.append('来源：'+' · '.join(group));pending_size+=length+2
                    html.append('<p style="font-size:13px">'+rendered+'</p>')
        links.append((doc['title'],settings.base_url+'/daily/'+key+'/'))
    block('网页阅读','b');html.append('<hr><p>网页阅读</p>')
    for title,url in links:
        block(title+'\n'+url)
        html.append('<p><a href="'+escape(url,quote=True)+'">'+escape(title)+'</a></p>')
    flush()
    body=''.join(html)
    return {'text':'\n\n'.join(plain),'html':'<!doctype html><html lang="zh-CN"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>'+
            '<body style="margin:0;padding:20px;color:#222;background:#fff"><main style="max-width:720px;margin:auto;font:16px/1.8 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;overflow-wrap:anywhere">'+body+'</main></body></html>',
            'telegram':parts}

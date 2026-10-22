"""Reader-facing news bodies shared by email and Telegram notifications."""
from __future__ import annotations

from html import escape

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import ROOT
from .reading import report_view

EMAIL_TEMPLATES = Environment(loader=FileSystemLoader(ROOT / 'templates'),
                              autoescape=select_autoescape(['html']), trim_blocks=True, lstrip_blocks=True)


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


def news_message(settings, documents: list[tuple[str,dict]], subject: str, *, notice: str = '',
                 bilingual_sections: set[str] | None = None) -> dict:
    plain=[];parts=[];pending=[];pending_size=0;links=[];views=[]

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
    if notice:
        block(notice)
    for key,doc in documents:
        view=report_view(doc,key,settings)
        views.append(view)
        if doc['title'] != subject or len(documents) > 1:
            block('━ '+doc['title']+' ━','b')
        if view['window']:
            block('报道时段：'+view['window'],'i')
        if not view['count']:
            block('本期未收录新闻。')
        for section in view['sections']:
            bilingual=section['id'] in (bilingual_sections or set())
            section['bilingual']=bilingual
            block('【'+section['name']+'】','b')
            for article in section['articles']:
                if bilingual:
                    from .translation import validate_bilingual
                    validate_bilingual(article,required=True)
                    block(article['bilingual']['title_en'],'b')
                block(article['title'],'b')
                meta=article['time']
                if article.get('importance'):meta+=' · '+article['importance']+' · 可靠性 '+article['reliability']
                if article['follow_up']:meta+=' · 进展'
                block(meta,'i')
                if bilingual:
                    for pair in article['bilingual_paragraphs']:
                        pair_size=utf16_length(pair['en'])+utf16_length(pair['zh'])+4
                        if pair_size<=3800 and pending_size+pair_size>3800:flush()
                        block(pair['en']);block(pair['zh'])
                else:
                    for paragraph in article['paragraphs']:
                        block(paragraph)
                if article['references']:
                    refs=article['references']
                    plain.append('来源：'+' · '.join(r['label']+' '+r['url'] for r in refs))
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
                # Keep each article's sources with its text, before the optional photo.
                flush()
                image=article.get('image')
                if image:
                    caption=article['title']+'\n'+image['caption']
                    parts.append({'method':'sendPhoto','photo':image['url'],
                                  'caption':escape(text_chunks(caption,900)[0])+'\n<a href="'+
                                  escape(image['source_url'],quote=True)+'">图片来源</a>','parse_mode':'HTML'})
        links.append((doc['title'],settings.base_url+'/daily/'+key+'/'))
    flush()
    block('网页阅读','b')
    for title,url in links:
        block(title+'\n'+url)
    flush()
    html=EMAIL_TEMPLATES.get_template('notification.html').render(
        subject=subject, notice=notice, views=views, links=links,
        site_name=settings.values.get('project',{}).get('name','Newspaper'))
    return {'text':'\n\n'.join(plain),'html':html,'telegram':parts}

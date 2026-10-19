"""Calendar schedules; each slot has a stable publication key and a full lookback."""
from __future__ import annotations

import hashlib
import json
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']


def validate_schedule(cfg: dict) -> None:
    if cfg.get('period', 'daily') not in {'daily', 'weekly', 'monthly', 'interval'}:
        raise ValueError('Schedule period must be daily, weekly, monthly or interval')
    clock = time.fromisoformat(cfg.get('time', '12:00'))
    if clock.tzinfo is not None or clock.second or clock.microsecond:
        raise ValueError('Schedule time must be local HH:MM')
    if cfg.get('weekday', 'mon') not in WEEKDAYS:
        raise ValueError('Schedule weekday must be mon through sun')
    if type(cfg.get('month_day', 1)) is not int or not 1 <= cfg.get('month_day', 1) <= 31:
        raise ValueError('Schedule month_day must be 1 through 31')
    if type(cfg.get('every_days', 1)) is not int or not 1 <= cfg.get('every_days', 1) <= 30:
        raise ValueError('Schedule every_days must be 1 through 30')
    if cfg.get('start_date'):
        date.fromisoformat(cfg['start_date'])
    if cfg.get('period') == 'interval' and not cfg.get('start_date'):
        raise ValueError('Interval schedule requires start_date')


def task_identity(cfg: dict, timezone: str) -> str:
    period=cfg.get('period','daily')
    value={'period':period,'time':time.fromisoformat(cfg.get('time','12:00')).strftime('%H:%M'),'timezone':timezone}
    for key in ({'weekly':['weekday'],'monthly':['month_day'],'interval':['every_days','start_date']}.get(period,[])):
        value[key]=cfg.get(key, {'weekday':'mon','month_day':1,'every_days':1}.get(key))
    if cfg.get('start_date'):value['start_date']=cfg['start_date']
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()[:20]


def previous_slot(end: datetime, cfg: dict) -> datetime:
    period = cfg.get('period', 'daily')
    if period == 'monthly':
        year, month = (end.year-1, 12) if end.month == 1 else (end.year, end.month-1)
        return end.replace(year=year, month=month, day=min(cfg.get('month_day', 1), monthrange(year, month)[1]))
    days = 7 if period == 'weekly' else cfg.get('every_days', 1) if period == 'interval' else 1
    # Calendar arithmetic preserves the configured local wall clock across DST.
    return end - timedelta(days=days)


def latest_window(now: datetime, cfg: dict, timezone: str) -> dict | None:
    validate_schedule(cfg)
    zone = ZoneInfo(timezone);now = now.astimezone(zone)
    clock = time.fromisoformat(cfg.get('time', '12:00'))
    end = datetime.combine(now.date(), clock, zone)
    period = cfg.get('period', 'daily')
    if period == 'weekly':
        end -= timedelta(days=(end.weekday()-WEEKDAYS.index(cfg.get('weekday', 'mon'))) % 7)
    elif period == 'monthly':
        end = end.replace(day=min(cfg.get('month_day', 1), monthrange(end.year, end.month)[1]))
    elif period == 'interval':
        anchor = date.fromisoformat(cfg['start_date']);days = (now.date()-anchor).days
        if days < 0:return None
        end = datetime.combine(anchor+timedelta(days=days//cfg.get('every_days',1)*cfg.get('every_days',1)),clock,zone)
    if end > now:end = previous_slot(end, cfg)
    if cfg.get('start_date') and end.date() < date.fromisoformat(cfg['start_date']):return None
    return {'date':end.date().isoformat(), 'window_start':previous_slot(end,cfg).isoformat(), 'window_end':end.isoformat()}


def jobs(settings, now: datetime, manual: bool = False) -> list[tuple[dict,dict]]:
    default = settings.values.get('schedule', {})
    timezone = settings.values.get('project', {}).get('timezone', 'UTC')
    result=[]
    for group in settings.editions:
        batches={}
        group_schedule={**default, **group.get('schedule', {})}
        for section in settings.sections:
            if section['id'] not in group['sections']:continue
            cfg={**group_schedule, **section.get('schedule', {})}
            notify=section.get('notify',group.get('notify',True))
            signature=json.dumps([task_identity(cfg,timezone),notify], sort_keys=True)
            batches.setdefault(signature, (cfg, [], notify))[1].append(section)
        for cfg, sections, notify in batches.values():
            if manual:
                end=now.astimezone(ZoneInfo(timezone)).replace(microsecond=0)
                window={'date':end.date().isoformat(),'window_start':previous_slot(end,cfg).isoformat(),
                        'window_end':end.isoformat()}
            else:window=latest_window(now,cfg,timezone)
            if window is None:continue
            ids=[s['id'] for s in sections]
            if task_identity(cfg,timezone) == task_identity(group_schedule,timezone) and notify==group.get('notify',True):slug=group['id']
            else:
                tail=ids[0] if len(ids)==1 else hashlib.sha256('|'.join(sorted(ids)).encode()).hexdigest()[:10]
                slug=group['id']+'-'+tail
                if len(slug)>40:slug=group['id'][:24]+'-'+hashlib.sha256(slug.encode()).hexdigest()[:12]
            # A generated job ID must not alias another top-level group's publication.
            if slug != group['id'] and slug in {g['id'] for g in settings.editions}:
                raise ValueError('Scheduled publication id collides with a group id')
            job={**group,'id':slug,'sections':ids,'schedule':cfg,'group_id':group['id'],'notify':notify,'task_id':task_identity(cfg,timezone)}
            if slug != group['id']:job['name']=group['name']+' · '+' / '.join(s['name'] for s in sections)
            if manual:
                suffix='-manual-'+end.strftime('%H%M%S')
                base=slug if len(slug+suffix)<=40 else slug[:16]+'-'+hashlib.sha256(slug.encode()).hexdigest()[:8]
                job['id']=base+suffix
                job['name']+=' · 手动刊'
            result.append((job,window))
    return result

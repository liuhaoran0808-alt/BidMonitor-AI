"""Source identity and conservative candidate matching; amounts are never entity IDs."""
import hashlib
import json
import re

META = {'查询企业','记录类别','企业匹配','源站页码','金额说明','来源类型','公告检索地址',
        '统一社会信用代码','来源内容SHA256','身份说明','身份依据','待核实说明'}

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def identity_for(record):
    fields = record['fields']
    project = fields.get('项目编号') or fields.get('招标项目编号')
    lot = fields.get('标段编号') or fields.get('包号') or fields.get('标包编号')
    source_id = fields.get('源站记录ID')
    if source_id:
        return fingerprint(['source',source_id]), 'source'
    if project and lot:
        return fingerprint(['lot',project,lot,fields.get('中标日期') or fields.get('开标时间')]), 'source'
    stable = {k:v for k,v in fields.items() if k not in META and not re.search('金额|报价|价格', k)}
    return fingerprint([record['title'],stable]), 'candidate'

def substantive(fields):
    return {k:v for k,v in fields.items() if k not in META}

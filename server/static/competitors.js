// 企业汇总保留各来源明细，不把模糊命中数或项目业绩计为开标记录。
let competitorLoaded = false, competitorTimer = null, competitorBusy = false;
const competitorHomeURLs = {};
const competitorStates = {
    pending:'等待', running:'查询中', completed:'已完成', company_only:'仅企业信息', partial:'部分读取成功',
    no_results:'源站无匹配记录', verification_required:'需要人工验证', unconfirmed:'未确认匹配记录',
    login_required:'需要登录', query_failed:'查询失败／匹配待确认', site_error:'源站访问异常', stopped:'已停止'
};
const competitorKinds = {company:'企业基本信息',project:'项目业绩',opening:'开标报价记录',tender:'招投标／中标成交记录'};

async function competitorAPI(path, body) {
    const response = await fetch(API + '/api/competitors/' + path, body === undefined ? {} : {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入或后端日志');
    return data;
}

function competitorSafeURL(value) {
    try {
        const url = new URL(value);
        if (!['https:','http:'].includes(url.protocol)) return '#';
        return escapeHtml(url.href).replace(/"/g,'&quot;');
    } catch (e) { return '#'; }
}

function competitorPrimaryField(fields, keys) {
    for (const key of keys) {
        const found = Object.keys(fields || {}).find(k => k.replace(/\s/g,'') === key || k.replace(/\s/g,'').startsWith(key+'（') || k.replace(/\s/g,'').startsWith(key+'('));
        if (found && String(fields[found]).trim()) return [found, String(fields[found])];
    }
    return null;
}

function competitorRecordKey(r) {
    return encodeURIComponent(r.record_key || JSON.stringify([r.company,r.record_type,r.title,r.url,r.fields]));
}

function renderCompetitorGroups(result) {
    const summary = result.summary || {groups:[],type_counts:{}};
    const groups = summary.groups || [];
    return groups.map(group => {
        const rows = result.items.filter(r => r.company === group.company);
        const counts = group.counts;
        const basic = rows.filter(r=>r.record_type==='company');
        const fieldValues = new Map();
        basic.forEach(r=>Object.entries(r.fields || {}).forEach(([key,value])=>{
            if (!fieldValues.has(key)) fieldValues.set(key,new Map());
            const values=fieldValues.get(key), text=String(value);
            if (!values.has(text)) values.set(text,new Set());
            values.get(text).add(r.source);
        }));
        const facts = [...fieldValues].map(([key,values])=>`<div><strong>${escapeHtml(key)}：</strong>${[...values].map(([value,sources])=>`${escapeHtml(value)} <small>（${escapeHtml([...sources].join('、'))}）</small>`).join('<br>')}</div>`).join('');
        const sections = ['project','opening','tender'].map(kind=>{
            const records=rows.filter(r=>r.record_type===kind);
            const dateOrder=r=>{const f=competitorPrimaryField(r.fields,['中标日期','成交日期','开标时间','开标日期','开工日期']);return f&&/^\d{4}-\d{2}-\d{2}/.test(f[1])?f[1]:'';};
            records.sort((a,b)=>dateOrder(b).localeCompare(dateOrder(a)));
            const body=records.map(r=>{
                const amount=competitorPrimaryField(r.fields,kind==='opening'?['投标报价','投标总报价']:kind==='tender'?['中标金额','中标（成交）金额','成交金额','中标总金额']:['合同金额']);
                if(amount&&kind==='opening'&&r.fields['报价单位'])amount[0]+='（'+String(r.fields['报价单位'])+'）';
                const date=competitorPrimaryField(r.fields,kind==='opening'?['开标时间','开标日期']:kind==='tender'?['中标日期','成交日期','公告日期']:['开工日期','完工日期']);
                const fieldList=Object.entries(r.fields || {}).map(([k,v])=>`<div>${escapeHtml(k)}：${escapeHtml(String(v))}</div>`).join('');
                const observations=(r.observations||[]).map(o=>`<div style="border-top:1px solid #ddd;margin-top:8px"><strong>${o.reason==='superseded'?'历史版本':'待核实差异（未覆盖原记录，不计新增）'}</strong><small> ${escapeHtml(o.collected_at||'')}</small>${Object.entries(o.fields||{}).map(([k,v])=>`<div>${escapeHtml(k)}：${escapeHtml(String(v))}</div>`).join('')}</div>`).join('');
                const review=(r.observations||[]).some(o=>o.reason!=='superseded')?' · 有差异待核实':'';
                const formatField=field=>field?`<small>${escapeHtml(field[0])}</small><br>${escapeHtml(field[1])}`:'未提供';
                const discovery=r.fields['公告检索地址']?`<br><a href="${competitorSafeURL(r.fields['公告检索地址'])}" target="_blank" rel="noopener">按项目检索原公告（非已核实原文链接）</a>`:'';
                return `<tr><td>${escapeHtml(r.title)}</td><td>${formatField(date)}</td><td>${formatField(amount)}</td><td>${escapeHtml(r.source)}<br><small>添加：${escapeHtml(r.collected_at||'未知')}</small></td><td><details data-cmp-key="${competitorRecordKey(r)}"><summary>查看明细${review}</summary><div style="min-width:220px;line-height:1.8">${fieldList}${observations}<a href="${competitorSafeURL(r.url)}" target="_blank" rel="noopener">查看来源（可能为企业成交汇总页）</a>${discovery}</div></details></td></tr>`;
            }).join('');
            return `<h4 style="margin:16px 0 8px">${competitorKinds[kind]} · ${counts[kind]||0}条来源明细</h4>${records.length?`<div style="overflow-x:auto"><table class="cmp-table"><thead><tr><th>项目／标段</th><th>业务日期</th><th>金额（原字段）</th><th>来源／添加时间</th><th>详情</th></tr></thead><tbody>${body}</tbody></table></div>`:'<p class="hint">暂无已保存明细。未采集到不等同于企业没有此类记录。</p>'}${(counts[kind]||0)>records.length?`<p class="hint">此分类已保存${counts[kind]}条，当前仅展示最近${records.length}条；页面总展示上限${result.display_limit}条。</p>`:''}`;
        }).join('');
        return `<section class="card" style="margin:14px 0"><h3>${escapeHtml(group.company)}</h3><p class="hint">已保存来源：${group.sources.map(s=>escapeHtml(s.name)).join('、')||'暂无'} · 最近添加：${escapeHtml(group.latest||'无')}</p><h4>企业基本信息</h4>${facts||'<p class="hint">暂无企业基本信息明细</p>'}${sections}</section>`;
    }).join('') || '<p class="hint">尚无已保存企业记录，请结合平台状态判断查询情况。</p>';
}

async function loadCompetitors() {
    try {
        if (!competitorLoaded) {
            const data=await competitorAPI('config');
            document.getElementById('cmpCompany').value=data.company_text||'';
            data.available.forEach(p=>{competitorHomeURLs[p.id]=p.url;});
            document.getElementById('cmpPlatforms').innerHTML=data.available.map(p=>`<label style="display:block;padding:8px 0"><input type="checkbox" name="cmpPlatform" value="${p.id}" ${data.platforms.includes(p.id)?'checked':''}> ${escapeHtml(p.name)}</label>`).join('');
            document.getElementById('cmpStates').addEventListener('click',async event=>{
                const button=event.target.closest('button[data-platform]');if(!button)return;
                button.disabled=true;
                try {const data=await competitorAPI('manual',{platform:button.dataset.platform,company:decodeURIComponent(button.dataset.company)});alert(data.message);await refreshCompetitors();}
                catch(e){alert(e.message);}finally{button.disabled=false;}
            });
            competitorLoaded=true;
        }
        await refreshCompetitors();
        if(!competitorTimer)competitorTimer=setInterval(refreshCompetitors,4000);
    }catch(e){document.getElementById('cmpStates').textContent=e.message;}
}

async function startCompetitorQuery() {
    const company_text=document.getElementById('cmpCompany').value.trim();
    const platforms=[...document.querySelectorAll('input[name="cmpPlatform"]:checked')].map(el=>el.value);
    const notice_urls=(document.getElementById('cmpNoticeURLs').value||'').split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
    if(!company_text||!platforms.length){alert('请填写企业全称并选择平台');return;}
    document.getElementById('cmpStart').disabled=true;
    try {await competitorAPI('query',{company_text,platforms,notice_urls});await refreshCompetitors();if(!competitorTimer)competitorTimer=setInterval(refreshCompetitors,4000);}
    catch(e){alert(e.message);document.getElementById('cmpStart').disabled=false;}
}

async function stopCompetitorQuery() {
    try{await competitorAPI('stop',{});await refreshCompetitors();}catch(e){alert(e.message);}
}

async function refreshCompetitors() {
    if(competitorBusy)return;competitorBusy=true;
    try{
        const state=await competitorAPI('status');
        document.getElementById('cmpStart').disabled=state.running||state.manual_opening;
        document.getElementById('cmpStop').disabled=!state.running;
        document.getElementById('cmpProgress').textContent=`${state.running?'查询中':'当前无运行任务'} · ${state.completed}/${state.total}`;
        document.getElementById('cmpStates').innerHTML=state.entries.length?state.entries.map(e=>{
            const manual = ['login_required','verification_required'].includes(e.status);
            return `<div class="result-item"><strong>${escapeHtml(e.source)}</strong><div>${escapeHtml(e.company)} · ${escapeHtml(competitorStates[e.status]||e.status)}</div><div class="hint">${escapeHtml(e.message)} · 读取${e.result_count}条 / 新增${e.new_count}条 / 招投标明细${e.opening_count}条</div><a href="${competitorSafeURL(e.query_url)}" target="_blank" rel="noopener">查看本次查询地址</a> · <a href="${competitorSafeURL(competitorHomeURLs[e.platform])}" target="_blank" rel="noopener">官网首页（普通浏览器）</a>${manual?` <button class="btn btn-outline" data-platform="${e.platform}" data-company="${encodeURIComponent(e.company)}" ${state.running||state.manual_opening?'disabled':''}>打开官网登录/验证窗口</button>`:''}</div>`;
        }).join(''):'<p class="hint">尚未执行查询。登录或验证码不会被当作“无记录”。</p>';
        const company_text=document.getElementById('cmpCompany').value.trim();
        const result=await competitorAPI('results?company_text='+encodeURIComponent(company_text));
        document.getElementById('cmpToday').textContent=result.today_new;
        document.getElementById('cmpTotal').textContent=result.total;
        const summary=result.summary,counts=summary.type_counts;
        document.getElementById('cmpSummary').textContent=`企业 ${summary.company_count} 家 · 项目业绩 ${counts.project} 条 · 开标报价 ${counts.opening} 条 · 招投标／中标成交 ${counts.tender} 条。${summary.count_note} · 待核实差异 ${result.review_count||0} 条（不计新增）。`;
        const target=document.getElementById('cmpResults');
        const opened=new Set([...target.querySelectorAll('details[open][data-cmp-key]')].map(el=>el.dataset.cmpKey));
        target.innerHTML=renderCompetitorGroups(result);
        target.querySelectorAll('details[data-cmp-key]').forEach(el=>{if(opened.has(el.dataset.cmpKey))el.open=true;});
        if(!state.running&&!document.getElementById('page-competitors').classList.contains('active')){clearInterval(competitorTimer);competitorTimer=null;}
    }catch(e){document.getElementById('cmpStates').textContent=e.message;}
    finally{competitorBusy=false;}
}

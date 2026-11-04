import {useEffect, useRef, useState} from 'react';
import {ArrowDownToLine, ArrowUpRight, Globe2, Loader2, Search, Square} from 'lucide-react';

type Source={id:string;title:string;image:string;url:string;price_min?:number|null;price_max?:number|null;observed_at?:string};
type Match={id:string;title:string;image:string;url:string;price_min:number;price_max:number;price_text:string;sales_text:string;observed_at:string};
type Item={source:Source;keyword:string;status:string;message:string;candidates_count:number;excluded:Record<string,number>;matches:Match[];checkpoint?:{rows:unknown[];resolved_positions:number[]}|null};
type Job={id:string;status:string;message:string;created_at:string;updated_at:string;items:Item[];events?:{time:string;message:string}[]};
type Session={browser?:string;mode:string;verified:boolean;retry_after?:number;verified_at?:string;navigation_interval?:number;candidate_limit?:number};
type Opportunity={source:Source;competitor:Match;source_rank:number;competitor_rank:number;reference_cost:number;reference_price:number;gap_min:number;gap_max:number;verification:string};
const active=['queued','running','paused'];
const names:Record<string,string>={queued:'等待采集',running:'后台采集中',paused:'需要处理',completed:'已完成',failed:'未完成',cancelled:'已停止',interrupted:'服务曾中断'};
const keyword=(title:string)=>title.replace(/一件代发|厂家直供|源头工厂|工厂直销|跨境专供|批发|现货/g,'').replace(/\s+/g,' ').trim().slice(0,80)||title.slice(0,80);
const money=(n:number)=>`¥${n.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}`;
const time=(s:string)=>new Date(s).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
function link(value:string,source=false){try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password&&(source?u.hostname==='detail.1688.com':u.hostname==='mobile.yangkeduo.com')?value:undefined}catch{return undefined}}
function picture(value:string){if(/^\/demo-assets\/[a-z-]+\.svg$/.test(value))return value;try{return new URL(value).protocol==='https:'?value:undefined}catch{return undefined}}
async function request<T>(path:string,body?:unknown):Promise<T>{
  let r:Response;try{r=await fetch('/api'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Workbench':'1'},...(body===undefined?{}:{body:JSON.stringify(body)})})}catch{throw new Error('无法连接本机工作台，请检查是否已启动')}
  if(!r.ok){const e=await r.json().catch(()=>({detail:'请求未完成，请重试'}));throw new Error(typeof e.detail==='string'?e.detail:'请求资料不正确')}
  return r.json();
}

export function CompetitionPanel({demo=false,taskId,products,sourceBusy,sourceState,sourceMessage,searchKeyword,onSourceResume,onSourceCancel}:{demo?:boolean;taskId:string;products:Source[];sourceBusy:boolean;sourceState?:string;sourceMessage?:string;searchKeyword?:string;onSourceResume?:()=>void;onSourceCancel?:()=>void}){
  const [jobs,setJobs]=useState<Job[]>([]),[selected,setSelected]=useState('');
  const [session,setSession]=useState<Session>({mode:'closed',verified:false});
  const [rows,setRows]=useState<Opportunity[]>([]),[minimum,setMinimum]=useState(0);
  const [keywords,setKeywords]=useState<Record<string,string>>({});
  const [error,setError]=useState(''),[busy,setBusy]=useState(false);
  const [gapExcluded,setGapExcluded]=useState<Record<string,number>>({});
  const generation=useRef(0);
  const shownJob=useRef('');
  const job=jobs.find(j=>j.id===selected)||jobs[0];
  const running=!!job&&active.includes(job.status);
  const top=products.slice(0,5),sourceKey=top.map(p=>p.id+p.title).join('|');
  const visible=rows.filter(r=>r.gap_min>minimum);
  useEffect(()=>{setKeywords(Object.fromEntries(top.map(p=>[p.id,keyword(p.title)])))},[taskId,sourceKey]);
  useEffect(()=>{
    const current=++generation.current;let stopped=false;let timer:ReturnType<typeof setTimeout>;
    setJobs([]);setSelected('');setRows([]);setError('');setBusy(false);
    async function load(){
      try{const [list,s]=await Promise.all([request<Job[]>('/competitions?source_task_id='+encodeURIComponent(taskId)),request<Session>('/pdd/session')]);if(!stopped&&generation.current===current){setJobs(list);setSession(s)}}catch(e){if(!stopped)setError((e as Error).message)}
      if(!stopped)timer=setTimeout(load,3000);
    }
    load();return()=>{stopped=true;clearTimeout(timer)};
  },[taskId]);
  useEffect(()=>{
    let ignore=false;if(shownJob.current!==(job?.id||'')){shownJob.current=job?.id||'';setRows([])}
    if(job)request<{rows:Opportunity[];excluded:Record<string,number>}>(`/competitions/${job.id}/opportunities`).then(r=>{if(!ignore){setRows(r.rows);setGapExcluded(r.excluded)}}).catch(e=>{if(!ignore)setError(e.message)});
    return()=>{ignore=true};
  },[job?.id,job?.updated_at]);
  async function act(fn:()=>Promise<void>){const current=generation.current;setBusy(true);setError('');try{await fn();const [list,s]=await Promise.all([request<Job[]>('/competitions?source_task_id='+encodeURIComponent(taskId)),request<Session>('/pdd/session')]);if(current===generation.current){setJobs(list);setSession(s)}}catch(e){if(current===generation.current)setError((e as Error).message)}finally{if(current===generation.current)setBusy(false)}}
  function start(){act(async()=>{const j=await request<Job>('/competitions',{source_task_id:taskId,keywords});setSelected(j.id)})}
  const platformBusy=job?.status==='running'||job?.status==='queued';
  const safari=session.browser==='Safari';
  const resultLink=(value:string,source=false)=>demo?undefined:link(value,source);
  const sessionLabel=demo?'演示数据 · 已就绪':(session.retry_after||0)>0?'平台暂忙，已停止请求':session.verified?(safari?'Safari 窗口采集已就绪':'Chrome 无头已就绪'):safari?'Safari 连接待验证':session.browser==='Chrome'?'Chrome 后台待验证':'正在读取采集状态';
  return <section className="panel competition-panel" aria-label="比价候选">
    <div className="section-heading"><div><Search size={18}/><h2>比价候选</h2><span className={`badge ${session.verified?'green':'neutral'}`}>{sessionLabel}</span>{job&&<span className="badge neutral">{names[job.status]}</span>}</div><button className="button small-button" disabled={busy||platformBusy||demo} onClick={()=>act(async()=>{await request('/pdd/browser/open',{})})}><Globe2 size={14}/>{demo?'演示无需登录':safari?'查看 Safari / 登录':'登录拼多多'}</button></div>
    <div className="competition-body">
      <div className="comparison-toolbar"><p>1688 取 20 条货源，前 5 条各查最多 5 个竞品。只展示有当前单品销量、且货价差为正的候选。</p><button className="button small-button" disabled={busy||platformBusy||demo||(session.retry_after||0)>0} onClick={()=>act(async()=>{await request('/pdd/session/confirm',{keyword:searchKeyword?.trim()||undefined});if(job?.status==='paused')await request(`/competitions/${job.id}/resume`,{})})}>{busy?<Loader2 size={14} className="spin"/>:<Globe2 size={14}/>}{demo?'演示无需验证':(session.retry_after||0)>0?`等待约${session.retry_after}秒后验证`:safari?`验证 Safari${job?.status==='paused'?'并继续':'连接'}`:`验证登录${job?.status==='paused'?'并继续':'并转后台'}`}</button></div>
      {(error||job?.status==='paused'||job?.status==='failed')&&<div className="alert error" role="alert">{error||job?.message}</div>}
      {sourceBusy&&<div className="comparison-status">{sourceState==='paused'?<><span>{sourceMessage||'1688需要登录或验证，请先打开登录浏览器处理。'}</span><button className="button small-button" onClick={onSourceResume}>已处理，继续1688</button></>:<><Loader2 size={14} className="spin"/><span>正在采集1688；完成后自动比价，结果会陆续保留。</span></>}{onSourceCancel&&<button className="text-button" onClick={onSourceCancel}>停止本次筛选</button>}</div>}
      {running&&<div className="comparison-status">{job.status==='running'&&<Loader2 size={14} className="spin"/>}<span>{job.status==='paused'?'已暂停，完成结果和当前断点已保存。':'正在限速后台采集，过程可展开复盘。'}</span><button className="text-button" disabled={busy} onClick={()=>act(async()=>{await request(`/competitions/${job.id}/cancel`,{})})}><Square size={12}/>停止</button></div>}
      <div className="comparison-filters"><label>最低货价差（元）<input type="number" aria-label="最低货价差" min="0" step="0.01" value={minimum} onChange={e=>setMinimum(Math.max(0,Number(e.target.value)||0))}/></label><span className="muted tiny">{visible.length} 条候选 · 当前单品销量</span>{job&&visible.length>0&&<a className="button small-button" href={`/api/competitions/${job.id}/opportunities/export?minimum_gap=${minimum}`}><ArrowDownToLine size={14}/>导出筛选结果</a>}</div>
      <p className="comparison-basis">货价差 = 拼多多最低展示价 − 1688最高展示价。尚未扣费用，不能视为净利润；同款、规格和报价条件请核对。销量仅在本次读取范围内排序。</p>
      {visible.length>0?<div className="table-wrap"><table className="comparison-results"><thead><tr><th>1688货源 / 报价上限</th><th>拼多多竞品 / 售价下限</th><th>当前单品销量</th><th>保守货价差</th></tr></thead><tbody>{visible.map(r=><tr key={r.source.id+':'+r.competitor.id}>
        <td><div className="comparison-goods">{picture(r.source.image)&&<img src={picture(r.source.image)} alt="1688货源图片" referrerPolicy="no-referrer"/>}<div><a href={resultLink(r.source.url,true)} target="_blank" rel="noreferrer">{r.source.title}<ArrowUpRight size={12}/></a><strong>{money(r.reference_cost)}</strong>{r.source.observed_at&&<small>{time(r.source.observed_at)}</small>}</div></div></td>
        <td><div className="comparison-goods">{picture(r.competitor.image)&&<img src={picture(r.competitor.image)} alt="拼多多竞品图片" referrerPolicy="no-referrer"/>}<div><a href={resultLink(r.competitor.url)} target="_blank" rel="noreferrer">{r.competitor.title}<ArrowUpRight size={12}/></a><strong>{money(r.reference_price)}</strong><small>{time(r.competitor.observed_at)}</small></div></div></td>
        <td><strong>{r.competitor.sales_text}</strong><small>统计周期以网页原文为准</small></td><td><strong className="gap-value">{money(r.gap_min)}</strong><small>同款规格待核对</small></td>
      </tr>)}</tbody></table></div>:<p className="competition-empty">{running||sourceBusy?'当前尚无已完成并通过价差条件的结果。':job?'当前没有超过货价差门槛的可比候选，可展开复盘查看未通过原因。':'输入关键词开始小批量筛选，或用当前货源前5件比价。'}</p>}
      <details className="comparison-audit"><summary>采集设置与过程复盘</summary>
        <p className="muted tiny">{demo?'当前仅使用虚构示例资料，商品链接不可跳转，不启动浏览器。下述访问间隔仅适用于真实采集，演示不执行平台请求。':safari?'沿用Safari自身登录会话，在独立窗口采集；自动操作不主动切到前台。Safari不是无头浏览器。':'同一专用Chrome资料保存登录状态，可见登录后验证无头访问。'}页面跳转操作至少间隔6秒，关键词至少间隔20秒。每词最多20个候选、10次详情核验，优先前列。登录校验或访问限制出现时停止请求；间隔不保证免除平台验证。</p>
        <div className="comparison-audit-actions"><button className="button small-button primary" disabled={busy||running||sourceBusy||!top.length} onClick={start}>重新比价前 {top.length||5} 件</button>{job?.status==='interrupted'&&<button className="button small-button" disabled={busy} onClick={()=>act(async()=>{await request(`/competitions/${job.id}/resume`,{})})}>从断点继续</button>}{job&&job.items.some(i=>i.matches.length)&&<a className="button small-button" href={`/api/competitions/${job.id}/export`}>导出完整竞品</a>}</div>
        {!running&&top.map((p,i)=><label className="comparison-keyword" key={p.id}><span>{i+1}. {p.title}</span><input aria-label={`第${i+1}件搜索关键词`} maxLength={80} value={keywords[p.id]??keyword(p.title)} onChange={e=>setKeywords(k=>({...k,[p.id]:e.target.value}))}/></label>)}
        {jobs.length>0&&<select className="competition-history" aria-label="切换竞品记录" value={job?.id||''} onChange={e=>setSelected(e.target.value)}>{jobs.map(j=><option key={j.id} value={j.id}>{time(j.created_at)} · {names[j.status]}</option>)}</select>}
        {job&&<><p>{job.message}</p><p className="tiny muted">价差筛选排除：{Object.entries(gapExcluded).map(([k,v])=>`${k} ${v}条`).join(' · ')||'无'}{rows.length>visible.length?` · 当前门槛另隐藏 ${rows.length-visible.length} 条`:''}</p>{job.items.map((item,i)=><div key={item.source.id} className="comparison-audit-item"><strong>{i+1}. {item.source.title}</strong><p>{item.message} · 候选 {item.candidates_count||item.checkpoint?.rows.length||0} 个 · 结果 {item.matches.length} 条</p>{item.matches.map((m,n)=><p key={m.id}>{n+1}. <a href={resultLink(m.url)} target="_blank" rel="noreferrer">{m.title}</a> · {money(m.price_min)} · {m.sales_text}</p>)}{Object.keys(item.excluded).length>0&&<small>{Object.entries(item.excluded).map(([k,v])=>`${k} ${v}件`).join(' · ')}</small>}</div>)}<ol className="comparison-events">{job.events?.map((e,i)=><li key={i}><time>{time(e.time)}</time> {e.message}</li>)}</ol></>}
      </details>
    </div>
  </section>;
}

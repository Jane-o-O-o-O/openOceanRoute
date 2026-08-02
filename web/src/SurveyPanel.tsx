import {useMemo, useRef, useState} from 'react';
import {Check, Download, FlaskConical, MapPinned, Play, Upload} from 'lucide-react';
import {request} from './api';
import {NumberField} from './fields';
import MapView from './MapView';
import type {Analysis, Layer, Project} from './types';

type Props = {project: Project; onApply: (project: Project) => void; notify: (message: string, error?: boolean) => void};
type Row = Record<string, any>;
const defaults = {source: '现场调查', observation_kind: 'cable_position_survey', vertical_datum: 'unspecified',
  depth_datums_aligned: false, max_gap_m: 10000, max_deviation_m: 50, max_match_distance_m: 10000,
  route_sample_step_m: 1000, station_tolerance_m: .01, ambiguity_distance_m: .1, ambiguity_kp_separation_m: 100,
  max_work_evaluations: 2000000, geometry_step_m: 1000, max_output_vertices: 100000};
const layerDefinitions = [
  {key: 'observed_points', name: '观测点', kind: 'survey_observations', color: '#617aa5'},
  {key: 'measured_line', name: '连续实測线', kind: 'survey_measured', color: '#0c9690'},
  {key: 'residual_vectors', name: '最近匹配残差', kind: 'survey_residuals', color: '#bc5865'},
] as const;
const summaryLabels: Record<string, string> = {
  observation_count: '调查点数', matched_count: '匹配点数', unmatched_count: '未匹配点数', ambiguous_count: '路线 KP 歧义点数',
  deviation_exceeded_count: '横偏超阈值点数', deviation_stat_count: '横偏统计有效点数', mean_signed_cross_track_m: '平均有符号横偏 / m',
  rms_cross_track_m: '横偏 RMS / m', max_abs_cross_track_m: '最大绝对横偏 / m', q95_abs_cross_track_m: '绝对横偏 P95 / m',
  continuous_chain_count: '连续调查链数', complete_observation_chain: '调查链完整', complete_observed_depth: '调查测深完整',
  complete_observed_cable_stations: '实测物理里程完整', measured_surface_length_m: '完整调查水平长 / m',
  measured_bottom_length_m: '完整实测底距 / m', measured_cable_length_m: '完整实测实物长 / m',
  known_measured_surface_length_m: '已知片段水平长 / m', known_measured_bottom_length_m: '已知片段底距 / m',
  known_measured_cable_length_m: '已知片段实物长 / m', comparable_segment_count: '可配对规划区间数',
};
const observedColumns = [
  ['id', '观测 ID'], ['route_kp_m', '最近规划 KP / m'], ['cable_kp_m', '实测物理 KP / m'],
  ['matched', '匹配'], ['ambiguity', 'KP 歧义'], ['cross_track_m', '有符号横偏 / m'], ['nearest_distance_m', '最短残差 / m'],
  ['along_track_residual_m', '沿向残差 / m'], ['observed_depth_m', '实测水深 / m'], ['planned_depth_m', '规划水深 / m'],
  ['depth_difference_m', '水深差 / m'], ['cable_kp_difference_m', '物理 KP 差 / m'],
] as const;
const segmentColumns = [
  ['index', '区间'], ['connected', '调查连续'], ['comparable', '可规划配对'],
  ['measured_surface_length_m', '实测水平 / m'], ['measured_bottom_length_m', '实测底距 / m'],
  ['measured_cable_length_m', '实测实物 / m'], ['measured_surface_slack_pct', '实测平面余缆 / %'],
  ['measured_bottom_slack_pct', '实测底余缆 / %'], ['planned_bottom_length_m', '规划底距 / m'],
  ['planned_cable_length_m', '规划实物 / m'], ['bottom_slack_difference_pct', '底余缆差 / 百分点'],
] as const;
function format(value: any): string {
  if (value == null) return '—';
  if (typeof value === 'number') return value.toLocaleString('zh-CN', {maximumFractionDigits: 4});
  if (typeof value === 'boolean') return value ? '是' : '否';
  return String(value);
}
function objectJSON(text: string): Row {
  const value = JSON.parse(text);
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('对账配置须为 JSON 对象');
  return value;
}
function download(text: string, name: string, type = 'application/json') {
  const url = URL.createObjectURL(new Blob([text], {type}));
  const link = document.createElement('a'); link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 3000);
}
function csv(rows: Row[], fields: string[]): string {
  const escape = (value: any) => {
    let text = value == null ? '' : String(value);
    if (typeof value === 'string' && /^[=+\-@]/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  };
  return '\ufeff' + [fields.join(','), ...rows.map(row => fields.map(field => escape(row[field])).join(','))].join('\r\n');
}
function Table({rows, columns, title}: {rows: Row[]; columns: readonly (readonly [string, string])[]; title: string}) {
  const [page, setPage] = useState(0), pages = Math.max(1, Math.ceil(rows.length/100)), current = Math.min(page, pages-1);
  return <section className="settings-card"><div className="section-heading"><h3>{title}</h3><span>{rows.length} 行 · 第 {current+1}/{pages} 页</span></div>
    <div className="table-wrap"><table><thead><tr>{columns.map(([key, label]) => <th key={key}>{label}</th>)}</tr></thead>
      <tbody>{rows.slice(current*100, current*100+100).map((row, i) => <tr key={row.id ?? row.index ?? i}>
        {columns.map(([key]) => <td key={key}>{format(row[key])}</td>)}</tr>)}</tbody></table></div>
    {pages>1 && <div className="page-actions"><button className="secondary-button" disabled={!current} onClick={() => setPage(current-1)}>上一页</button>
      <button className="secondary-button" disabled={current>=pages-1} onClick={() => setPage(current+1)}>下一页</button></div>}</section>;
}

export default function SurveyPanel({project, onApply, notify}: Props) {
  const latestProject = useRef(project); latestProject.current = project;
  const [inputKind, setInputKind] = useState<'text'|'json'>('text');
  const [input, setInput] = useState('longitude,latitude,depth_m,cable_kp_m,id,break_before\n');
  const [configText, setConfigText] = useState(JSON.stringify(defaults, null, 2));
  const [stationMode, setStationMode] = useState('relative'), [offset, setOffset] = useState(0);
  const [result, setResult] = useState<Row|null>(null), [resultProject, setResultProject] = useState<Project|null>(null);
  const [geometry, setGeometry] = useState<number[][]|undefined>(), [resultSignature, setResultSignature] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [name, setName] = useState('');
  const [visible, setVisible] = useState<Record<string, boolean>>({observed_points: true, measured_line: true, residual_vectors: true});
  const config = useMemo(() => {try {return objectJSON(configText);} catch {return null;}}, [configText]);
  const signature = JSON.stringify({project, inputKind, input, configText, stationMode, offset});
  const stale = !!result && signature !== resultSignature;
  function changeConfig(key: string, value: any) {
    if (!config) {setError('请先修正完整配置 JSON'); return;}
    setConfigText(JSON.stringify({...config, [key]: value}, null, 2));
  }
  async function load(file?: File) {
    if (!file) return;
    if (file.size>2000000) {setError('调查文件最多 2 MB，请按测区拆分'); return;}
    try {
      setInput(await file.text()); setInputKind(file.name.toLowerCase().endsWith('.json')?'json':'text'); setName(file.name); setError('');
    } catch (cause) {setError(cause instanceof Error?cause.message:String(cause));}
  }
  async function run() {
    setBusy(true); setError('');
    const snapshot = signature, source = structuredClone(project);
    try {
      const payload: Row = {...objectJSON(configText)};
      delete payload.observations; delete payload.text; delete payload.cable_kp_offset_m;
      if (inputKind==='json') {
        const observations = JSON.parse(input); if (!Array.isArray(observations)) throw new Error('观测 JSON 须为数组');
        payload.observations = observations;
      } else payload.text = input;
      if (stationMode==='aligned') payload.cable_kp_offset_m = offset;
      const [data, analysis] = await Promise.all([
        request<Row>('/survey/reconcile', {project: source, config: payload}), request<Analysis>('/analyze', source),
      ]);
      if (latestProject.current!==project) throw new Error('工程已在计算期间修改，请重新对账');
      setResult(data); setResultProject(source); setGeometry(analysis.route_geometry?.coordinates); setResultSignature(snapshot);
      notify('实敷调查对账完成，测量成果可预览或单独加入工程图层');
    } catch (cause) {
      const message = cause instanceof Error?cause.message:String(cause); setError(message); notify(message, true);
    } finally {setBusy(false);}
  }
  const surveyLayers = useMemo<Layer[]>(() => result ? layerDefinitions.map(def => ({
    id: 'survey-preview-'+def.key, name: def.name, kind: def.kind, visible: !!visible[def.key],
    geojson: result.geojson_layers[def.key],
  })) : [], [result, visible]);
  const preview = useMemo(() => resultProject ? {...resultProject, layers: [...resultProject.layers, ...surveyLayers]} : null, [resultProject, surveyLayers]);
  function applyLayers() {
    if (!result || stale) return;
    const next = structuredClone(project);
    next.layers.push(...surveyLayers.map(layer => ({...structuredClone(layer), id: 'survey-'+crypto.randomUUID(),
      name: `${result.source.name} · ${layer.name}`, geojson: {...structuredClone(layer.geojson),
        survey_metadata: {source: result.source, model: result.model, summary: result.summary, planned_route_signature: result.comparison.planned_route_signature}}})));
    onApply(next); notify('测量成果已加入图层，设计路线及制造量保持原账目');
  }
  const downloadResult = () => download(JSON.stringify(result, null, 2), 'OceanRoute-as-laid-reconciliation.json');
  function downloadCSV() {
    if (!result) return;
    const rows = result.observations.map((row: Row) => ({...row, source: result.source.name, vertical_datum: result.source.vertical_datum,
      depth_datums_aligned: result.source.depth_datums_aligned, cable_kp_offset_m: result.source.cable_kp_offset_m}));
    download(csv(rows, ['id','longitude','latitude',...observedColumns.slice(1).map(([key])=>key),'nearest_longitude','nearest_latitude',
      'planned_cable_kp_m','chain_index','chain_surface_kp_m','note','source','vertical_datum','depth_datums_aligned','cable_kp_offset_m']),
      'OceanRoute-as-laid-observations.csv', 'text/csv;charset=utf-8');
  }
  return <main className="engineering-main survey-panel"><div className="page-heading"><div><small>INDEPENDENT AS-LAID RECONCILIATION</small>
    <h2>实敷调查对账</h2><p>实际观测位置与规划 WGS84 曲线对照，保留缺测、空档和路线 KP 歧义。</p></div>
    <button className="primary-button" disabled={busy} onClick={run}><Play size={14}/>{busy?'正在实际对账…':'计算调查对账'}</button></div>
    <div className="explanation"><FlaskConical size={15}/>研究模型：最近匹配不能证明海床接触或埋深。规划平面 KP、调查水平里程和实测物理缆 KP 是三套不同账目；回环歧义不会自动消除。</div>
    <section className="settings-card"><h3>调查来源与坐标</h3><div className="tool-fields">
      <label>输入格式<select value={inputKind} onChange={e=>setInputKind(e.target.value as 'text'|'json')}><option value="text">CSV / TSV / 分号文本</option><option value="json">observations JSON 数组</option></select></label>
      <label>来源名称<input value={config?.source??''} onChange={e=>changeConfig('source',e.target.value)}/></label>
      <label>观测类型<select value={config?.observation_kind??'cable_position_survey'} onChange={e=>changeConfig('observation_kind',e.target.value)}><option value="cable_position_survey">调查的实际缆位置</option><option value="reported_touchdown">用户报告触地点</option><option value="vessel_track">船位轨迹（仅几何）</option></select></label>
    </div><label className="file-drop"><Upload size={18}/><span>{name||'上传 CSV、TSV、TXT 或观测 JSON'}</span><input type="file" accept=".csv,.tsv,.txt,.json" onChange={e=>void load(e.target.files?.[0])}/></label>
      <label className="text-config">调查数据<textarea aria-label="实敷调查数据" value={input} onChange={e=>setInput(e.target.value)} spellCheck={false} rows={9}/></label>
      <p className="chart-meta">坐标为 WGS84 十进制度；水深正向下、单位米。必需 longitude/latitude；可选 depth_m/cable_kp_m/id/time_s/break_before。按正敷设方向输入，物理 KP 非缺测值须递增；空档可用 break_before=true，缺测值留空或 null。</p>
    </section><section className="settings-card"><h3>匹配、空档和里程基准</h3><div className="tool-fields">
      {([['max_gap_m','最大连续观测间距 / m',.01],['max_deviation_m','横偏警告阈值 / m',0],['max_match_distance_m','最大匹配半径 / m',.01],['route_sample_step_m','路线搜索分段 / m',25]] as const).map(([key,label,min])=>
        <label key={key}>{label}<NumberField aria-label={label} value={config?.[key]} min={min} disabled={!config} onChange={v=>changeConfig(key,v)}/></label>)}
      <label>实测物理 KP 对照<select value={stationMode} onChange={e=>setStationMode(e.target.value)}><option value="relative">原点未对齐 · 只算实测区间长度</option><option value="aligned">显式对齐原点 · 同时算绝对物理 KP 差</option></select></label>
      {stationMode==='aligned'&&<label>对齐偏置 / m<NumberField aria-label="实物里程对齐偏置 / m" value={offset} onChange={v=>setOffset(v??0)}/></label>}
      <label>测量垂直基准<input value={config?.vertical_datum??''} onChange={e=>changeConfig('vertical_datum',e.target.value)}/></label>
    </div><label className="checkbox-label"><input type="checkbox" checked={!!config?.depth_datums_aligned} disabled={!config} onChange={e=>changeConfig('depth_datums_aligned',e.target.checked)}/>明确测量与规划水深采用共同垂直基准，计算点水深差</label>
      <p className="chart-meta">路线 KP 由实际曲线最近点计算。对齐物理 KP = 实测物理 KP + 偏置；没有物理测量值时不从规划自动生成。超过间距、显式断开和缺深不外推，完整总长显示 —。</p>
      <details className="advanced-environment"><summary>数值预算与完整配置 JSON</summary><textarea aria-label="调查对账配置 JSON" value={configText} onChange={e=>setConfigText(e.target.value)} rows={12} spellCheck={false}/><p className="chart-meta">最多 5,000 观测点、20,000 路线搜索区间，计算预算和输出顶点有明确上限。station_tolerance_m 是数值容差，不能作为测量精度。observations/text 由上方输入提供；物理原点偏置由显式对齐模式控制。</p></details>
    </section>{error&&<div className="inline-warning" role="alert">{error}</div>}
    {result&&preview?<><div className="section-heading"><h3>真实调查结果</h3><div className="page-actions"><button className="secondary-button" onClick={downloadResult}><Download size={14}/>结果 JSON</button>
      <button className="secondary-button" onClick={downloadCSV}><Download size={14}/>观测对账 CSV</button><button className="primary-button" disabled={stale||busy} onClick={applyLayers}><Check size={14}/>将测量成果加入工程图层</button></div></div>
      {stale&&<div className="inline-warning">工程或调查输入已更改，下面显示此前计算；请重新对账后再加入图层。</div>}
      <div className="tool-summary">{Object.entries(summaryLabels).map(([key,label])=><div key={key}><span>{label}</span><strong>{format(result.summary[key])}</strong></div>)}</div>
      <div className="page-actions">{layerDefinitions.map(def=><label className="checkbox-label" key={def.key}><input type="checkbox" checked={visible[def.key]} onChange={e=>setVisible({...visible,[def.key]:e.target.checked})}/><span style={{color:def.color}}>●</span>{def.name}</label>)}</div>
      <div className="routing-map" style={{height:420}}><MapView project={preview} geometry={geometry} selected={null} onSelect={()=>{}} onMove={()=>{}} onAdd={()=>{}} fitVersion={0} editable={false} allowAdd={false}/></div>
      <p className="chart-meta">地图展示计算时的设计路线、调查点、连续测量线和已匹配残差。— 表示不适用或缺测；横偏右侧为正，端点同时显示沿向残差。歧义点保留最早 KP 供定位，但不参与区间自动对账。</p>
      <Table rows={result.observations} columns={observedColumns} title="观测点与规划最近匹配"/>
      <Table rows={result.segments} columns={segmentColumns} title="相邻实测区间账目"/>
      <div className="page-actions"><button className="secondary-button" onClick={()=>download(csv(result.segments, Object.keys(result.segments[0]||{index:0})), 'OceanRoute-as-laid-segments.csv', 'text/csv;charset=utf-8')}><Download size={14}/>区间对账 CSV</button></div>
      {(result.warnings||[]).map((warning:Row,i:number)=><div className="inline-warning" key={i}>{warning.code} · {warning.message}</div>)}
      <details className="assumptions"><summary>实际模型、来源与假设</summary><p>{result.model.identity} · {result.model.validation_status}</p><p>{JSON.stringify(result.source)}</p>{result.assumptions?.map((text:string,i:number)=><p key={i}>{text}</p>)}</details>
    </>:<div className="tool-empty"><MapPinned size={30}/><span>导入真实调查数据后计算；成果可独立查看、导出或加入工程图层。</span></div>}
  </main>;
}

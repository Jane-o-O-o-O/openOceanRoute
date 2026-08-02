import {useEffect, useMemo, useState} from 'react';
import {Download, FlaskConical, Pause, Play, RotateCcw, Settings2, Waves} from 'lucide-react';
import {request} from './api';
import {NumberField} from './fields';
import Scene3D from './Scene3D';
import type {Frame, Project} from './types';

type Mode = 'generate' | 'simulate' | 'montecarlo';
type Row = Record<string, any>;
type Props = {project: Project; onMessage?: (message: string) => void};
const tabs: {id: Mode; name: string; sub: string}[] = [
  {id: 'generate', name: '波谱与时间序列', sub: 'SEA STATE'},
  {id: 'simulate', name: '海况动态', sub: 'SEA DYNAMICS'},
  {id: 'montecarlo', name: 'Monte Carlo', sub: 'ERROR BUDGET'},
];
const smallSimulation = {
  depth_m: 30, duration_s: 2, nodes: 8, dt_s: .25, internal_dt_s: .05,
  bottom_tension_n: 100, ship_speed_m_s: 0, heading_deg: 90, payout_m_s: 0,
  wet_weight_n_m: 4, mass_kg_m: 1, diameter_m: .02, drag_coefficient: 1.2,
  ea_n: 1e8, damping_ratio: .15, seabed_friction: .4, solver_iterations: 20,
};
const exampleRAO = [{frequency_hz: .25, amplitude_m_m: 1, phase_deg: 0}];
const exampleUncertainties = [
  {scope: 'simulation', parameter: 'current_y_m_s', distribution: 'uniform', lower: -.3, upper: .3},
  {scope: 'sea_state', parameter: 'rao_scale', distribution: 'normal', lower: .8, upper: 1.2, mean: 1, std: .1},
];
const labels: Record<string, string> = {
  depth_m: '水深 / m', duration_s: '计算时长 / s', nodes: '缆节点数', dt_s: '输出帧间隔 / s',
  internal_dt_s: '内部步长 / s', bottom_tension_n: '初始底张力 / N', ship_speed_m_s: '船速 / m·s⁻¹',
  heading_deg: '船艏向 / °', payout_m_s: '放缆速度 / m·s⁻¹', current_x_m_s: 'X 向海流 / m·s⁻¹',
  current_y_m_s: 'Y 向海流 / m·s⁻¹', wet_weight_n_m: '缆湿重 / N·m⁻¹', mass_kg_m: '干质量 / kg·m⁻¹',
  diameter_m: '缆直径 / m', ea_n: '轴向刚度 EA / N', hs_m: '波高 Hs / m', tp_s: '峰值周期 Tp / s',
  wave_direction_deg: '波传播方向 / °', seed: '波相位种子', sample_dt_s: '海况采样间隔 / s',
  component_count: '离散波分量数', gamma: 'JONSWAP γ', frequency_min_hz: '最低频率 / Hz',
  frequency_max_hz: '最高频率 / Hz', rao_scale: '用户 RAO 缩放', spectral_m0_m2: '谱零阶矩 m₀ / m²',
  spectral_hm0_m: '谱显著波高 Hm₀ / m', sample_surface_std_m: '记录波面标准差 / m',
  peak_heave_displacement_m: '船端最大升沉位移 / m', mean_bottom_tension_n: '窗口平均底张力 / N',
  peak_top_tension_n: '峰值顶张力 / N', peak_segment_tension_n: '峰值缆段张力 / N',
  minimum_bend_radius_m: '最小弯曲半径 / m', paid_out_m: '本次放缆 / m', final_touchdown: '最终触地点 / m',
  touchdown_shift_m: '触地点平面偏移 / m', requested_runs: '请求试验数', successful_runs: '完成试验数',
  converged_runs: '收敛试验数', failed_runs: '失败试验数', complete: '全部试验收敛',
  converged: '求解收敛', estimated_work_units: '估计工作量', initial_heave_dc_shift_m: '初始升沉平移 / m',
};
function format(value: any): string {
  if (value == null) return '—';
  if (typeof value === 'number') return Number.isFinite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits: 5}) : '—';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (Array.isArray(value)) return value.map(format).join(', ');
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}
function objectJSON(text: string, label: string): Row {
  const value = JSON.parse(text);
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error(label + '须为 JSON 对象');
  return value;
}
function arrayJSON(text: string, label: string): Row[] {
  const value = JSON.parse(text);
  if (!Array.isArray(value)) throw new Error(label + '须为 JSON 数组');
  return value;
}
function Summary({values, title}: {values?: Row; title: string}) {
  return <section className="sim-results"><div className="panel-heading">{title}</div>
    <div className="summary-fields">{Object.entries(values || {}).map(([key, value]) =>
      <div key={key}><span>{labels[key] || key}</span><strong>{format(value)}</strong></div>)}</div></section>;
}
function Warnings({values}: {values?: any[]}) {
  return <>{values?.map((warning, i) => <div className="inline-warning" key={i}>
    {typeof warning === 'string' ? warning : `${warning.code ? warning.code + ' · ' : ''}${warning.message || JSON.stringify(warning)}`}
  </div>)}</>;
}
function Plot({points, xLabel, yLabel, title, color = '#0ea5a4', stems = false}:
  {points: [number, number][]; xLabel: string; yLabel: string; title: string; color?: string; stems?: boolean}) {
  const valid = points.filter(p => p.every(Number.isFinite));
  if (!valid.length) return <div className="chart-meta">{title}：此结果没有对应数据。</div>;
  let loX = Math.min(...valid.map(p => p[0])), hiX = Math.max(...valid.map(p => p[0]));
  let loY = Math.min(0, ...valid.map(p => p[1])), hiY = Math.max(0, ...valid.map(p => p[1]));
  if (loX === hiX) {loX = Math.max(0, loX - .05); hiX += .05;}
  if (loY === hiY) {loY -= .1; hiY += .1;}
  const px = (x: number) => 70 + (x - loX) / (hiX - loX) * 680;
  const py = (y: number) => 220 - (y - loY) / (hiY - loY) * 160;
  return <section className="sim-results sea-plot"><div className="panel-heading">{title}</div>
    <svg viewBox="0 0 800 275" role="img" aria-label={title} style={{width: '100%', minHeight: 200}}>
      {[0, .25, .5, .75, 1].map(t => <g key={t}>
        <line x1="70" y1={60 + 160 * t} x2="750" y2={60 + 160 * t} stroke="#b9ccd5" opacity=".5"/>
        <text x="60" y={64 + 160 * t} textAnchor="end" fontSize="12" fill="#526c7b">{format(hiY - (hiY - loY) * t)}</text>
        <text x={70 + 680 * t} y="240" textAnchor="middle" fontSize="12" fill="#526c7b">{format(loX + (hiX - loX) * t)}</text>
      </g>)}
      <line x1="70" y1="60" x2="70" y2="220" stroke="#526c7b"/>
      <line x1="70" y1="220" x2="750" y2="220" stroke="#526c7b"/>
      {stems ? valid.map((p, i) => <g key={i}><line x1={px(p[0])} y1={py(0)} x2={px(p[0])} y2={py(p[1])} stroke={color} strokeWidth="3"/>
        <circle cx={px(p[0])} cy={py(p[1])} r="3" fill={color}/></g>) :
        <polyline points={valid.map(p => `${px(p[0])},${py(p[1])}`).join(' ')} fill="none" stroke={color} strokeWidth="2.5"/>}
      <text x="410" y="266" textAnchor="middle" fontSize="13" fill="#526c7b">{xLabel}</text>
      <text x="70" y="35" fontSize="13" fill="#526c7b">{yLabel}</text>
    </svg></section>;
}

export function SeaPanel({project, onMessage}: Props) {
  const [mode, setMode] = useState<Mode>('generate');
  const [sea, setSea] = useState<Row>({spectrum: 'regular', hs_m: .2, tp_s: 4, wave_direction_deg: 90,
    phase_deg: 0, seed: 42, sample_dt_s: .05, component_count: 16, gamma: 3.3,
    frequency_min_hz: .05, frequency_max_hz: 1, rao_scale: 1});
  const [simulationText, setSimulationText] = useState(JSON.stringify(smallSimulation, null, 2));
  const [raoText, setRaoText] = useState(JSON.stringify(exampleRAO, null, 2));
  const [customText, setCustomText] = useState(JSON.stringify([{frequency_hz: .25, amplitude_m: .1, phase_deg: 0, direction_deg: 90}], null, 2));
  const [uncertaintyText, setUncertaintyText] = useState(JSON.stringify(exampleUncertainties, null, 2));
  const [fixedHeave, setFixedHeave] = useState(false), [fluid, setFluid] = useState(true);
  const [runs, setRuns] = useState(3), [mcSeed, setMCSeed] = useState(42);
  const [varyPhases, setVaryPhases] = useState(true), [includeFrames, setIncludeFrames] = useState(true);
  const [result, setResult] = useState<Row | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [resultSignature, setResultSignature] = useState(''), [requestConfig, setRequestConfig] = useState<Row | null>(null);
  const [frameIndex, setFrameIndex] = useState(0), [playing, setPlaying] = useState(false), [trialIndex, setTrialIndex] = useState(0);
  const simulation = useMemo(() => {try {return objectJSON(simulationText, 'simulation');} catch {return null;}}, [simulationText]);
  const signature = JSON.stringify({mode, sea, simulationText, raoText, customText, uncertaintyText, fixedHeave, fluid,
    runs, mcSeed, varyPhases, includeFrames, project});
  const generated = mode === 'generate' ? result : mode === 'simulate' ? result?.sea_state : null;
  const selectedTrial = mode === 'montecarlo' ? result?.trials?.[trialIndex] : null;
  const dynamic = mode === 'simulate' ? result?.simulation : selectedTrial?.simulation;
  const frames: Frame[] = dynamic?.frames || [];
  const frame = frames[Math.min(frameIndex, Math.max(0, frames.length - 1))] || null;
  const depth = mode === 'simulate' ? result?.sea_state?.wave_kinematics?.depth_m ?? result?.effective_simulation_config?.depth_m :
    selectedTrial?.config?.simulation?.depth_m ?? (frame?.touchdown?.[2] != null ? Math.abs(frame.touchdown[2]) : undefined);
  useEffect(() => {
    if (!playing) return;
    const timer = setInterval(() => setFrameIndex(i => {
      if (i >= frames.length - 1) {setPlaying(false); return i;}
      return i + 1;
    }), 160);
    return () => clearInterval(timer);
  }, [playing, frames.length]);
  function updateSimulation(key: string, value: number | null) {
    if (!simulation) {setError('请先修正完整 simulation JSON，再编辑数值参数。'); return;}
    setSimulationText(JSON.stringify({...simulation, [key]: value ?? 0}, null, 2));
  }
  function resetExample() {
    setSea({spectrum: 'regular', hs_m: .2, tp_s: 4, wave_direction_deg: 90, phase_deg: 0, seed: 42,
      sample_dt_s: .05, component_count: 16, gamma: 3.3, frequency_min_hz: .05, frequency_max_hz: 1, rao_scale: 1});
    setSimulationText(JSON.stringify(smallSimulation, null, 2)); setRaoText(JSON.stringify(exampleRAO, null, 2));
    setUncertaintyText(JSON.stringify(exampleUncertainties, null, 2)); setFixedHeave(false); setFluid(true);
    setRuns(3); setMCSeed(42); setVaryPhases(true); setIncludeFrames(true); setResult(null); setPlaying(false); setError('');
  }
  async function run() {
    setBusy(true); setError(''); setPlaying(false);
    const snapshot = signature;
    try {
      const sim = objectJSON(simulationText, 'simulation');
      const waves: Row = {...sea, fixed_vessel_heave: fixedHeave, include_fluid_kinematics: fluid};
      if (!fixedHeave && raoText.trim()) {
        const table = arrayJSON(raoText, '用户 heave_rao');
        if (table.length) waves.heave_rao = table;
      }
      if (sea.spectrum === 'custom') waves.components = arrayJSON(customText, '波分量');
      if (mode !== 'generate' && !fixedHeave && !waves.heave_rao?.length)
        throw new Error('动态海况须提供有效用户 RAO，或明确选择固定船端升沉。缺表仍可生成波面。');
      if (mode === 'montecarlo' && includeFrames && runs > 8) throw new Error('包含全部帧的 Monte Carlo 最多 8 次试验。');
      const config = mode === 'generate' ? {...waves, duration_s: sim.duration_s, depth_m: sim.depth_m,
        ship_speed_m_s: sim.ship_speed_m_s ?? 0, heading_deg: sim.heading_deg ?? 90} :
        {simulation: sim, sea_state: waves, ...(mode === 'montecarlo' ? {runs, seed: mcSeed,
          uncertainties: arrayJSON(uncertaintyText, '不确定性声明'), vary_wave_phases: varyPhases, include_frames: includeFrames} : {})};
      const data = await request<Row>('/sea/' + mode, {project, config});
      setResult(data); setResultSignature(snapshot); setRequestConfig(config); setFrameIndex(0); setTrialIndex(0);
      onMessage?.('海况计算完成，结果来自实际求解。');
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause); setError(message); onMessage?.(message);
    } finally {setBusy(false);}
  }
  function download(value: unknown, name: string) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: 'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = name; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 3000);
  }
  const waveFields = sea.spectrum === 'custom' ? ['seed', 'sample_dt_s'] :
    ['hs_m', 'tp_s', 'wave_direction_deg', 'seed', 'sample_dt_s', 'rao_scale', ...(sea.spectrum === 'regular' ? ['phase_deg'] :
      ['component_count', 'frequency_min_hz', 'frequency_max_hz', ...(sea.spectrum === 'jonswap' ? ['gamma'] : [])])];
  return <div className="simulation-layout sea-layout"><aside className="sim-settings">
    <div className="section-label"><Waves size={15}/>海况与误差预算</div>
    <div className="solver-tabs">{tabs.map(tab => <button key={tab.id} className={mode === tab.id ? 'active' : ''}
      disabled={busy} onClick={() => {setMode(tab.id); setResult(null); setPlaying(false); setError('');}}>
      <strong>{tab.name}</strong><small>{tab.sub}</small></button>)}</div>
    <button className="secondary-button" onClick={resetExample} disabled={busy}>加载可复算的小算例</button>
    <label className="text-config">波谱模型<select value={sea.spectrum} onChange={e => setSea({...sea, spectrum: e.target.value})}>
      <option value="regular">规则波（hs 为峰谷高）</option><option value="jonswap">JONSWAP</option>
      <option value="pm">Pierson–Moskowitz</option><option value="custom">用户离散分量</option></select></label>
    <div className="sim-fields">{waveFields.map(key => <label key={key}>{labels[key] || (key === 'phase_deg' ? '规则波相位 / °' : key)}
      <NumberField value={sea[key]} onChange={value => setSea({...sea, [key]: value ?? 0})}/></label>)}</div>
    {sea.spectrum === 'custom' && <label className="text-config">用户波分量 JSON<textarea spellCheck={false}
      value={customText} onChange={e => setCustomText(e.target.value)}/></label>}
    <p className="chart-meta">方向表示从北顺时针的传播方位。JONSWAP/PM 按 Hs = 4√m₀ 归一；规则波 hs_m 是峰谷高。</p>
    <label className="checkbox-label"><input type="checkbox" checked={fixedHeave} onChange={e => setFixedHeave(e.target.checked)}/>
      明确固定船端升沉（不使用 RAO 表）</label>
    <label className="text-config">用户垂向 RAO JSON<textarea spellCheck={false} aria-label="用户垂向RAO JSON"
      value={raoText} disabled={fixedHeave} onChange={e => setRaoText(e.target.value)}
      placeholder="留空可生成波面，动态求解须给表或固定船端"/></label>
    <p className="chart-meta">初始表是幅值比 1、相位 0 的研究示例，未对应任何船型。频率 Hz、幅值 m/m、正相位表示滞后；宽带波需要表覆盖所有有效频率，禁止外推。</p>
    <div className="section-label"><Settings2 size={14}/>缆与船参数</div>
    <div className="sim-fields">{['depth_m', 'duration_s', 'nodes', 'dt_s', 'internal_dt_s', 'bottom_tension_n',
      'ship_speed_m_s', 'heading_deg', 'payout_m_s', 'current_x_m_s', 'current_y_m_s'].map(key =>
      <label key={key}>{labels[key]}<NumberField value={simulation?.[key]} onChange={value => updateSimulation(key, value)}/></label>)}</div>
    <details className="advanced-environment"><summary>完整动力学输入 JSON</summary>
      <label className="text-config">simulation<textarea spellCheck={false} aria-label="完整动力学输入 JSON"
        value={simulationText} onChange={e => setSimulationText(e.target.value)}/></label>
      <p className="chart-meta">波谱生成使用其中水深、时长和船速；动态／Monte Carlo 使用完整对象。缆材参数是显式研究输入，可在此填写材料区段、缆体和船舶计划。</p></details>
    {mode !== 'generate' && <label className="checkbox-label"><input type="checkbox" checked={fluid} onChange={e => setFluid(e.target.checked)}/>
      在线性水粒子速度中计算相对拖曳</label>}
    {mode === 'montecarlo' && <><div className="sim-fields">
      <label>实际试验次数（2～24）<NumberField value={runs} min={2} max={24} step={1} onChange={v => setRuns(v ?? 3)}/></label>
      <label>不确定性采样种子<NumberField value={mcSeed} min={0} step={1} onChange={v => setMCSeed(v ?? 42)}/></label></div>
      <label className="checkbox-label"><input type="checkbox" checked={varyPhases} onChange={e => setVaryPhases(e.target.checked)}/>
        各次试验改变随机波相位</label>
      <label className="checkbox-label"><input type="checkbox" checked={includeFrames} onChange={e => setIncludeFrames(e.target.checked)}/>
        返回各次真实三维帧（最多 8 次）</label>
      {varyPhases && <p className="chart-meta">各次试验实际重抽波相位，规则波重抽 phase_deg，用户分量逐项重抽；完整抽样输入保存在结果中。</p>}
      <label className="text-config">用户不确定性声明 JSON<textarea spellCheck={false} aria-label="不确定性声明 JSON"
        value={uncertaintyText} onChange={e => setUncertaintyText(e.target.value)}/></label>
      <p className="chart-meta">上下界是绝对参数值；normal 为有界正态的逆分布采样。最多 8 个独立参数声明，不假定设备误差、相关性或真实失效概率。</p></>}
    <button className="primary-button sim-run" onClick={run} disabled={busy}><Play size={15}/>{busy ? '正在实际计算…' : mode === 'generate' ? '生成波谱与序列' : mode === 'simulate' ? '运行海况动态' : '运行实际 Monte Carlo'}</button>
    <div className="research-note"><FlaskConical size={16}/><p>独立研究模型：只提供垂向 RAO，未求解船体六自由度。波流速度进入拖曳，未包含波浪惯性力、辐射、绕射或实海校准。</p></div>
  </aside><main className="sim-main">
    <div className="sim-heading"><div><small>WAVES & UNCERTAINTY</small><h2>{tabs.find(tab => tab.id === mode)?.name}</h2></div>
      <span className="tag warning">研究模型</span><button className="secondary-button" disabled={!result}
        onClick={() => download(result, `OceanRoute-sea-${mode}-result.json`)}><Download size={15}/>下载完整结果</button></div>
    {error && <div className="inline-warning" role="alert">{error}</div>}
    {result && signature !== resultSignature && <div className="inline-warning">输入已修改，当前视图和下载仍是上次计算；请重新运行。</div>}
    {!result && <div className="empty-state"><Waves size={32}/><h3>查看可复算的海况响应</h3>
      <p>填写波谱、用户 RAO 和动力学边界后计算。这里的时间序列、帧和试验分布均来自后端结果。</p></div>}
    {generated && <>
      <Summary title="海况摘要" values={generated.summary}/>
      {generated.spectrum?.length ? <Plot title="有限频带波谱" xLabel="频率 / Hz" yLabel="谱密度 / m²·Hz⁻¹"
        points={generated.spectrum.map((r: Row) => [r.frequency_hz, r.density_m2_hz])}/> :
        <Plot title="实际离散波分量" xLabel="频率 / Hz" yLabel="波幅 / m" stems
          points={(generated.components || []).map((r: Row) => [r.frequency_hz, r.amplitude_m])}/>}
      <Plot title="实际波面时间序列" xLabel="局部时间 / s" yLabel="波面高程 / m"
        points={(generated.samples || []).map((r: Row) => [r.time_s, r.surface_elevation_m])}/>
      {generated.vessel_motion_series ? <Plot title="用户 RAO 船端升沉序列" xLabel="局部时间 / s" yLabel="相对初始升沉 / m" color="#8b5cf6"
        points={generated.vessel_motion_series.map((r: Row) => [r.time_s, r.heave_m])}/> :
        <div className="inline-warning">没有用户 RAO，船端响应未计算。波面图可用；动态接口不会猜测船型响应。</div>}
      <details className="solver-diagnostics"><summary>波分量与相位输入</summary><pre>{JSON.stringify(generated.components, null, 2)}</pre></details>
    </>}
    {mode === 'montecarlo' && result && <>
      <Summary title="实际试验完成情况" values={result.summary}/>
      <Summary title="基准实际动力学指标" values={result.baseline_metrics}/>
      <div className="table-wrap"><table><thead><tr><th>模型输出</th><th>收敛样本数</th><th>均值</th><th>标准差</th><th>q05</th><th>q50</th><th>q95</th></tr></thead>
        <tbody>{Object.entries(result.distributions || {}).map(([key, value]) => {const d = value as Row; return <tr key={key}>
          <td>{labels[key] || key}</td><td>{format(d.count)}</td><td>{format(d.mean)}</td><td>{format(d.standard_deviation)}</td>
          <td>{format(d.q05)}</td><td>{format(d.q50)}</td><td>{format(d.q95)}</td></tr>;})}</tbody></table></div>
      <p className="chart-meta">分位数只来自收敛的实际试验，不是实船风险置信区间；失败或不收敛试验须单独审查。</p>
      <div className="table-wrap"><table><thead><tr><th>实际试验</th><th>收敛</th><th>平均底张力 / N</th><th>峰顶张力 / N</th><th>最小半径 / m</th><th>抽样输入</th></tr></thead>
        <tbody>{(result.trials || []).map((trial: Row, i: number) => <tr key={trial.trial} className={i === trialIndex ? 'selected' : ''}>
          <td><button className="secondary-button" onClick={() => {setTrialIndex(i); setFrameIndex(0); setPlaying(false);}}>试验 {trial.trial}</button></td>
          <td>{format(trial.solver?.converged)}</td><td>{format(trial.metrics?.mean_bottom_tension_n)}</td>
          <td>{format(trial.metrics?.peak_top_tension_n)}</td><td>{format(trial.metrics?.minimum_bend_radius_m)}</td>
          <td>{JSON.stringify(trial.parameters)}</td></tr>)}</tbody></table></div>
      {result.failures?.length > 0 && <details className="solver-diagnostics" open><summary>失败试验（{result.failures.length}）</summary>
        {result.failures.map((failure: Row) => <p className="inline-warning" key={failure.trial}>试验 {failure.trial}：{failure.error} · {JSON.stringify(failure.parameters)}</p>)}</details>}
    </>}
    {dynamic && <><div className="panel-heading">{mode === 'montecarlo' ? `试验 ${selectedTrial.trial} 的真实计算帧` : '海况驱动的真实计算帧'}</div>
      <div className="sim-view"><Scene3D frame={frame} depth={depth ?? simulation?.depth_m ?? 30}/>
        <div className="sim-overlay"><span>实际求解坐标 m · z 向上</span>{frame && <strong>t = {format(frame.time_s)} s</strong>}</div></div>
      <div className="playback"><button title="回到首帧" disabled={!frames.length} onClick={() => {setFrameIndex(0); setPlaying(false);}}><RotateCcw size={16}/></button>
        <button title="播放实际帧" disabled={frames.length < 2} onClick={() => setPlaying(!playing)}>{playing ? <Pause size={16}/> : <Play size={16}/>}</button>
        <input aria-label="海况计算帧" type="range" min={0} max={Math.max(0, frames.length - 1)} value={Math.min(frameIndex, Math.max(0, frames.length - 1))}
          disabled={!frames.length} onChange={e => {setPlaying(false); setFrameIndex(Number(e.target.value));}}/>
        <span>{frames.length ? `${frameIndex + 1} / ${frames.length} 帧` : '本次没有返回帧'}</span></div>
      <Summary title="实际动力学指标" values={mode === 'simulate' ? result?.metrics : selectedTrial?.metrics}/>
      <details className="solver-diagnostics"><summary>真实求解诊断</summary><Summary title="求解器" values={dynamic.solver}/></details>
      <Warnings values={mode === 'montecarlo' ? selectedTrial?.warnings : []}/>
    </>}
    {mode === 'montecarlo' && result && !dynamic && <div className="chart-meta">本次未包含帧；逐试验指标仍来自实际动力学。勾选返回帧并重新计算可查看三维节点。</div>}
    {result && <><Warnings values={result.warnings}/>
      <div className="assumptions"><strong>模型假设与限制</strong>{(result.assumptions || []).map((item: string, i: number) => <p key={i}>{item}</p>)}</div>
      <details className="solver-diagnostics"><summary>实际运行配置与下载</summary>
        <button className="secondary-button" onClick={() => download(mode === 'simulate' ? result.effective_simulation_config :
          mode === 'montecarlo' ? selectedTrial?.config ?? requestConfig : requestConfig, 'OceanRoute-sea-effective-config.json')}><Download size={14}/>下载实际配置</button>
        {mode === 'montecarlo' && !selectedTrial && <p className="chart-meta">没有成功试验，展示原请求基准输入；失败原因见试验记录。</p>}
        <pre>{JSON.stringify(mode === 'simulate' ? result.effective_simulation_config : mode === 'montecarlo' ? selectedTrial?.config ?? requestConfig : requestConfig, null, 2)}</pre>
      </details></>}
  </main></div>;
}
export default SeaPanel;

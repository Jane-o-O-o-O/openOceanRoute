import './equilibrium.css';
import {useEffect,useRef,useState} from 'react';
import {request} from './api';
import type {EquilibriumProvenance,Project} from './types';

const f=(v:any):string=>v==null?'不适用':typeof v==='number'?
  (v!==0&&Math.abs(v)<1e-6?v.toExponential(5):v.toLocaleString('zh-CN',{maximumFractionDigits:8})):
  typeof v==='boolean'?v?'是':'否':Array.isArray(v)?v.map(f).join(', '):String(v);

export function EquilibriumDiagnostics({value}:{value:any}){
  const proof:EquilibriumProvenance|undefined=value?.provenance||value?.initialization;
  if(!proof||!['oceanroute.dynamic.initial-equilibrium.provenance.v1','oceanroute.dynamic.initial-equilibrium.provenance.v2','oceanroute.dynamic.initial-equilibrium.provenance.v3'].includes(proof.schema))return null;
  const snapshot=proof.initial_snapshot,loading=proof.material_loading,fluid=proof.fluid_loading;
  const version2=proof.schema.endsWith('.v2');
  const summary:any[][]=[['初始活动自然库存 / m',proof.initial_material_length_m],['初态最老端制造里程 / m',proof.initial_suspended_material_m],['初态累计新放缆 / m',proof.initial_paid_out_m],['最大节点净力残差 / N',proof.verification?.max_node_force_residual_n],['力容限 / N',proof.verification?.force_tolerance_n],['初始化归一化计算额度',proof.solver?.estimated_work_units],['初始化额度上限',proof.solver?.max_work_units],['实际静力迭代',proof.solver?.iterations],['实际函数评估',proof.solver?.function_evaluations]];
  if(loading)summary.push(['初态总有符号湿重 / N',loading.signed_total_wet_weight_n],['绝对载荷尺度 / N',loading.absolute_load_scale_n],['已投入零长度点实体数',loading.point_bodies.filter(b=>b.deployed_fraction>0).length]);
  return <section className="sim-results equilibrium-diagnostics">
    <div className="panel-heading">实际定端初态求解与独立验收 <small>{fluid?'稳恒流 / 材料证据 v3':version2?'材料 / 点负载证据 v2':'同质无已投入实体证据 v1'}</small></div>
    <div className="summary-fields">{summary.map(([label,v])=><div key={label}><span>{label}</span><strong>{f(v)}</strong></div>)}</div>
    <p className="chart-meta">真实求解、几何与力验收通过后才进入积分。初态为零节点速度；船舶运动与放缆从第一个真实内部步开始。固定锚端可离床，实际无接触时触地点与底张力不适用。不是施工历史重建或现场精度证明。</p>
    {loading&&<p className="chart-meta equilibrium-loading-note">初态按自然材料区间积分缆湿重与串联柔度，再按制造位置把零长度点负载分配到节点。湿重正值向下、负值为浮力；节点负载保留符号，法向支持力为单侧非负量。点实体没有有限杆长、刚体姿态或形状碰撞效果。</p>}
    {fluid&&<section className="equilibrium-fluid-history">
      <div className="panel-heading">冻结的历史初始流场 <small>独立重构实际流与阻力，不从当前后续控制推猜</small></div>
      <div className="summary-fields"><div><span>初始水密度 / kg·m⁻³</span><strong>{f(fluid.initial_fluid.water_density_kg_m3)}</strong></div><div><span>声明恒定流 [X,Y,Z] / m·s⁻¹</span><strong>{f(fluid.initial_fluid.current_m_s)}</strong></div><div><span>实际初态流来源</span><strong>{fluid.initial_fluid.current_profile?'按模型深度的分层流，覆盖恒定流':'声明水平恒定流'}</strong></div></div>
      <p className="chart-meta">深度取 max(−模型 Z, 0)，分层流端点外保持端值；分层声明覆盖恒定流。初态节点速度为0。后续海流变化会产生真实瞬态，保留这份历史声明与初态载荷，不重新求解历史平衡。缆的法向阻力可有垂向分量；点实体采用各向同性阻力。</p>
      {fluid.initial_fluid.current_profile&&<div className="table-wrap equilibrium-fluid-profile"><table><thead><tr><th>初始深度 / m</th><th>X 向初始流 / m·s⁻¹</th><th>Y 向初始流 / m·s⁻¹</th></tr></thead><tbody>{fluid.initial_fluid.current_profile.map((row,i)=><tr key={i}><td>{f(row.depth_m)}</td><td>{f(row.x_m_s)}</td><td>{f(row.y_m_s)}</td></tr>)}</tbody></table></div>}
      <details className="equilibrium-fluid-nodes"><summary>初态逐节点流速、阻力与外载荷 · {fluid.node_fluid_velocity_m_s.length} 节点</summary><p className="chart-meta">X 东 / Y 北 / Z 上。阻力系数列已含水密度的一半及实际材料 / 点实体分配，单位 kg·m⁻¹；外载荷为缆阻力 + 点阻力 + [0,0,−有符号湿重]，尚不含内部张力或床法向支持。</p><div className="table-wrap"><table><thead><tr><th>节点 · 船→锚</th><th>初始流 [X,Y,Z] / m·s⁻¹</th><th>节点割线单位切向</th><th>缆阻力因子 / kg·m⁻¹</th><th>点阻力因子 / kg·m⁻¹</th><th>缆阻力 [X,Y,Z] / N</th><th>点阻力 [X,Y,Z] / N</th><th>真实外载荷 [X,Y,Z] / N</th></tr></thead><tbody>{fluid.node_fluid_velocity_m_s.map((u,i)=><tr key={i}><td>{i}</td><td>{f(u)}</td><td>{f(fluid.node_tangent[i])}</td><td>{f(fluid.node_cable_drag_factor[i])}</td><td>{f(fluid.node_body_drag_factor[i])}</td><td>{f(fluid.node_cable_drag_n[i])}</td><td>{f(fluid.node_body_drag_n[i])}</td><td>{f(fluid.node_external_force_n[i])}</td></tr>)}</tbody></table></div></details>
    </section>}
    {snapshot?.rest_lengths_m&&<details className="equilibrium-material-segments"><summary>初态逐段自然材料与真实载荷 · {snapshot.rest_lengths_m.length} 段</summary>
      <div className="table-wrap"><table><thead><tr><th>段 · 船→锚</th><th>制造坐标起 / m</th><th>制造坐标止 / m</th><th>自然长 / m</th><th>实际等效 EA / N</th>{loading&&<><th>缆段湿重 / N</th><th>串联柔度 / m·N⁻¹</th></>}<th>初态段张力 / N</th></tr></thead><tbody>{snapshot.rest_lengths_m.map((length,i)=><tr key={i}><td>{i}</td><td>{f(snapshot.node_material_m?.[i])}</td><td>{f(snapshot.node_material_m?.[i+1])}</td><td>{f(length)}</td><td>{f(snapshot.segment_ea_n?.[i])}</td>{loading&&<><td>{f(loading.segment_cable_wet_weight_n[i])}</td><td>{f(loading.segment_compliance_m_n[i])}</td></>}<td>{f(snapshot.segment_tension_n?.[i])}</td></tr>)}</tbody></table></div>
    </details>}
    {!!loading?.point_bodies?.length&&<details className="equilibrium-point-bodies"><summary>初态零长度点实体的实际节点分配 · {loading.point_bodies.length} 项</summary>
      <p className="chart-meta">来自实际材料载荷算子；分配系数对应船→锚的节点顺序，不把实体制造站位当成路线 KP。表中分配不表示刚性杆或接触历史。</p>
      <div className="table-wrap"><table><thead><tr><th>实体 ID</th><th>制造坐标 / m</th><th>声明长度 / m</th><th>干质量 / kg</th><th>有符号湿重 / N</th><th>已投入比例</th><th>实际节点分配系数</th></tr></thead><tbody>{loading.point_bodies.map(body=><tr key={body.id}><td>{body.id}</td><td>{f(body.material_m)}</td><td>{f(body.length_m)}</td><td>{f(body.mass_kg)}</td><td>{f(body.wet_weight_n)}</td><td>{f(body.deployed_fraction)}</td><td className="equilibrium-fractions">{f(body.node_fractions)}</td></tr>)}</tbody></table></div>
    </details>}
    {snapshot?.positions&&<details className="equilibrium-force-nodes"><summary>初态逐节点材料、湿重与真实力证据 · {snapshot.positions.length} 节点</summary>
      <p className="chart-meta">首尾是明确固定支持点。端反力含节点载荷，不能当成首末缆段张力；初态法向力是静力证据，不伪造时刻0的动态冲量。</p>
      <div className="table-wrap"><table><thead><tr><th>节点 · 船→锚</th><th>实际 [X,Y,Z] / m</th><th>制造坐标 / m</th><th>干质量 / kg</th><th>等效惯性质量 / kg</th>{loading&&<><th>缆湿重 / N</th><th>点实体湿重 / N</th></>}<th>总有符号湿重 / N</th><th>法向力 / N</th><th>端反力 [X,Y,Z] / N</th><th>净力残差 [X,Y,Z] / N</th></tr></thead><tbody>{snapshot.positions.map((p,i)=><tr key={i}><td>{i===0?'0 · 船端':i===snapshot.positions.length-1?i+' · 固定锚端':i}</td><td>{f(p)}</td><td>{f(snapshot.node_material_m?.[i])}</td><td>{f(snapshot.node_dry_mass_kg?.[i])}</td><td>{f(snapshot.node_mass_kg?.[i])}</td>{loading&&<><td>{f(loading.node_cable_wet_weight_n[i])}</td><td>{f(loading.node_body_wet_weight_n[i])}</td></>}<td>{f(snapshot.node_wet_weight_n?.[i])}</td><td>{f(snapshot.node_contact_normal_force_n?.[i])}</td><td>{f(snapshot.node_boundary_force_n?.[i])}</td><td>{f(snapshot.node_force_residual_n?.[i])}</td></tr>)}</tbody></table></div>
    </details>}
    <details><summary>完整初态来源、材料与力证据</summary><pre>{JSON.stringify(proof,null,2)}</pre></details>
  </section>;
}

type Props={project:Project;mode:string;onMode:(v:string)=>void;text:string;onText:(v:string)=>void;disabled:boolean;signature:string;buildConfig:()=>Record<string,any>;onExample:()=>void;onHeterogeneousExample?:()=>void;onCurrentExample?:()=>void};
export default function EquilibriumInitialPanel({project,mode,onMode,text,onText,disabled,signature,buildConfig,onExample,onHeterogeneousExample,onCurrentExample}:Props){
  const [preview,setPreview]=useState<any>(null),[previewSignature,setPreviewSignature]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const latest=useRef(signature);latest.current=signature;const mounted=useRef(true);useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[]);const current=preview&&signature===previewSignature;
  async function prepare(){const snapshot=signature;setBusy(true);setPreview(null);setError('');try{const result=await request<any>('/simulation/prepare-equilibrium-initial',{project,config:buildConfig()});if(mounted.current&&latest.current===snapshot){setPreview(result);setPreviewSignature(snapshot)}}catch(e){if(mounted.current&&latest.current===snapshot)setError(e instanceof Error?e.message:String(e))}finally{if(mounted.current)setBusy(false)}}
  function declareFluid(){try{const config=buildConfig(),raw=JSON.parse(text);raw.schema='oceanroute.dynamic.initial-equilibrium.v2';raw.initial_fluid={schema:'oceanroute.initial-fluid.v1',operator:'node-secant-normal-cable-and-isotropic-body-drag-v1',water_density_kg_m3:config.water_density_kg_m3,current_m_s:[config.current_x_m_s,config.current_y_m_s,0],current_profile:config.current_profile??null,depth_reference:'max(-model_z_m,0)',profile_extrapolation:'hold_endpoints'};onText(JSON.stringify(raw,null,2));onMode('current_equilibrium');setPreview(null);setError('')}catch(e){setError(e instanceof Error?e.message:String(e))}}
  const explicit=mode==='equilibrium'||mode==='current_equilibrium';
  return <details className="advanced-environment equilibrium-initial-settings" open={explicit}><summary>动态初态来源</summary>
    <label>新计算初态方法<select aria-label="动态初态方法" disabled={disabled} value={mode} onChange={e=>onMode(e.target.value)}><option value="approx">原水平悬链线 / 床面投影近似</option><option value="equilibrium">明确定端自然长 · 实际平衡求解</option><option value="current_equilibrium">明确定端稳恒流 · 实际平衡求解</option></select></label>
    {disabled&&<p>续算保留完整状态及原始初态证据，不重新求解或替换初态。</p>}
    {explicit&&<><label className="text-config">明确动态定端初态 JSON<textarea aria-label="明确动态定端初态 JSON" disabled={disabled} value={text} onChange={e=>onText(e.target.value)} spellCheck={false} placeholder={'{"schema":"oceanroute.dynamic.initial-equilibrium.v1","vessel_position_m":[0,0,0],"anchor_position_m":[-20,0,-10],"natural_length_m":25}'}/></label>
      <p className="chart-meta">配套完整二维床格，坐标均为其局部米。声明自然材料长或逐段自然长；节点草图只作数值初值。允许分区正湿重 / EA、已投入零长度点实体（点湿重可负），活动缆 EI=0；已投入有限长实体 / 杆与初态波流仍拒绝。未来未投入混材与有限实体由动态材料模型处理。</p>
      <p className="chart-meta">原 raw v1 仍要求恒定及每条分层流均为0，保持 proof v1/v2、model-v4 / checkpoint schema3。显式 raw v2 声明完整 initial_fluid，可求稳恒水平流或按模型深度的分层流平衡，使用 proof v3、model-v5 / checkpoint schema4。即使零流也不自动降级。材料与在线实体仍在主配置；数值 / 工作量 / JSON 体积超限明确拒绝。</p>
      {mode==='current_equilibrium'&&<><p className="chart-meta">初始流声明必须与当前水密度、X / Y 海流和分层流完全一致。以下操作仅生成新的输入声明，尚未验收；不会改写恢复状态中的历史流场。分层流覆盖恒定流。</p><button className="secondary-button" disabled={disabled||busy} onClick={declareFluid}>重新声明当前初始流场</button></>}
      <button className="secondary-button" disabled={disabled||busy} onClick={prepare}>{busy?'实际求解和验收中…':'预备并独立验证动态初态'}</button>
      {error&&<div className="inline-warning equilibrium-initial-error" role="alert">{error}</div>}{preview&&!current&&<p className="inline-warning">输入已变，旧初态证据隐藏，请重新预备。</p>}{current&&<EquilibriumDiagnostics value={preview}/>}</>}
    <button className="secondary-button" disabled={disabled} onClick={onExample}>加载明确合成定端平衡动态算例</button>
    {onHeterogeneousExample&&<button className="secondary-button" disabled={disabled} onClick={onHeterogeneousExample}>加载明确合成异质缆与零长度点实体初态</button>}
    {onCurrentExample&&<button className="secondary-button" disabled={disabled} onClick={onCurrentExample}>加载明确合成分层流与异质初态</button>}
  </details>;
}

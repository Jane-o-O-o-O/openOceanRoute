import {request} from './api';
import type {Analysis,Leg,Point,Project} from './types';

export type ProjectedInput={id:string;x:number;y:number};
export type AppendPointOptions={label:string;cable_type_id:string;mode:'flexible'|'fixed';fixed_cable_length_m?:number};
export type ProjectedPointPreview={transform:any;project:Project|null;constraint_report:any;constraint_warnings:any[]};
export class StaleProjectedEdit extends Error{constructor(){super('工作区、路径、修订或投影参数已改变，本次点位结果已丢弃');this.name='StaleProjectedEdit'}}
function current(check:()=>boolean){if(!check())throw new StaleProjectedEdit()}
export function projectedPointLocked(project:Project,pid:string){return !!project.route.constraint_state&&project.route.mode==='fixed'&&project.route.points.find(p=>p.id===pid)?.constraint==='sliding'}

/** Actual horizontal transform followed by the same manufacturing-domain solver as XY editing. */
export async function previewProjectedPointEdit(project:Project,sourceCRS:string,points:ProjectedInput[],check:()=>boolean=()=>true,append?:AppendPointOptions):Promise<ProjectedPointPreview>{
 current(check);
 if(append){
  if(project.route.constraint_state)throw new Error('请先明确解除旧制造域，再追加路线点');
  if(points.length!==1)throw new Error('地图追加一次只能提供一个点');
  if(!project.cable_types.some(c=>c.id===append.cable_type_id))throw new Error('请明确选择当前共享库中的缆型');
  if(append.mode==='fixed'&&(typeof append.fixed_cable_length_m!=='number'||!Number.isFinite(append.fixed_cable_length_m)||append.fixed_cable_length_m<0))throw new Error('固定新段必须明确填写非负制造缆长，不以零补缺');
 }else for(const p of points){if(!project.route.points.some(v=>v.id===p.id))throw new Error('点位必须属于当前活跃路径');if(projectedPointLocked(project,p.id))throw new Error('Fixed Sliding 点只能修改实物 KP，不能拖动或编辑 X/Y')}
 const transform=await request<any>('/coordinates/transform',{source_crs:sourceCRS,target_crs:'EPSG:4326',points,error_policy:'collect'});
 current(check);
 if(!transform.can_apply)return {transform,project:null,constraint_report:null,constraint_warnings:[]};
 const moves=transform.points.map((p:any)=>({point_id:p.id,longitude:p.output.x,latitude:p.output.y}));
 if(append){
  const candidate=structuredClone(project),move=moves[0];
  if(candidate.route.points.some(p=>p.id===move.point_id))throw new Error('新点标识已被使用');
  const point:Point={id:move.point_id,label:append.label.trim()||`WP-${candidate.route.points.length+1}`,longitude:move.longitude,latitude:move.latitude,depth_m:null,note:''};
  const oldLegs=candidate.route.points.slice(1).map((_,i)=>({...candidate.route.legs[i]}));
  candidate.route.points.push(point);
  candidate.route.legs=[...oldLegs,{cable_type_id:append.cable_type_id,mode:append.mode,slack_pct:candidate.route.slack_pct,slack_basis:candidate.route.slack_basis,burial:false,...(append.mode==='fixed'?{fixed_cable_length_m:append.fixed_cable_length_m}: {})} as Leg];
  return {transform,project:candidate,constraint_report:null,constraint_warnings:[]};
 }
 if(project.route.constraint_state){const solved=await request<any>('/constraints/edit',{project,config:{moves}});current(check);return {transform,project:solved.project,constraint_report:solved.report,constraint_warnings:solved.warnings||[]}}
 const candidate=structuredClone(project);for(const move of moves){const point=candidate.route.points.find(p=>p.id===move.point_id)!;point.longitude=move.longitude;point.latitude=move.latitude}
 return {transform,project:candidate,constraint_report:null,constraint_warnings:[]};
}

/** Deletion preserves declared quantities and rejects ambiguous merging rather than inventing zero lengths. */
export function removeRoutePoint(project:Project,pid:string,analysis:Analysis|null,analysisReady:boolean):Project{
 if(project.route.constraint_state)throw new Error('请先明确解除旧制造域，再删除路线点');
 if(project.route.points.length<=2)throw new Error('路线必须至少保留两个点');
 const index=project.route.points.findIndex(p=>p.id===pid);if(index<0)throw new Error('删除点不属于当前活跃路径');
 const candidate=structuredClone(project),legs=project.route.points.slice(1).map((_,i)=>({...project.route.legs[i]}));
 if(index>0&&index<project.route.points.length-1){
  const a=legs[index-1],b=legs[index],type=(l:Leg)=>l.cable_type_id||project.cable_types[0]?.id;
  if(type(a)!==type(b))throw new Error('相邻段缆型不同，不能用删点合并材料；请先明确调整分段');
  const fixed=(l:Leg)=>(l.mode||project.route.mode)==='fixed';
  const sumKeys=new Set(['allowance_m','stop_hours','extra_cost']);
  const derivedKeys=new Set(['mode','fixed_cable_length_m','cable_type_id',...sumKeys]);
  for(const key of new Set([...Object.keys(a),...Object.keys(b)])){
   if(derivedKeys.has(key))continue;
   const av=(a as any)[key],bv=(b as any)[key];
   const defaultValue=key==='slack_pct'?project.route.slack_pct:key==='slack_basis'?project.route.slack_basis:key==='burial'?false:undefined;
   if(av!==undefined&&bv!==undefined&&JSON.stringify(av)!==JSON.stringify(bv)||defaultValue!==undefined&&JSON.stringify(av??defaultValue)!==JSON.stringify(bv??defaultValue))throw new Error(`相邻段 ${key} 不同，删除会丢失分段信息；请先明确统一或使用工程工具`);
  }
  const merged={...b,...a};
  if(fixed(a)||fixed(b)){
   const length=(l:Leg,i:number)=>{if(fixed(l)){const value=l.fixed_cable_length_m;if(typeof value==='number'&&Number.isFinite(value)&&value>=0)return value;throw new Error('固定段缺少明确制造缆长，不能用零补缺')}
    const value=analysis?.legs[i]?.cable_length_m;if(!analysisReady||typeof value!=='number'||!Number.isFinite(value)||value<0)throw new Error('请等待当前路线分析完成，才能把柔性段合并为固定制造量');return value};
   merged.mode='fixed';merged.fixed_cable_length_m=length(a,index-1)+length(b,index);
  }
  for(const key of sumKeys){const av=(a as any)[key],bv=(b as any)[key];if(av===undefined&&bv===undefined)continue;for(const v of [av,bv])if(v!==undefined&&(typeof v!=='number'||!Number.isFinite(v)||v<0))throw new Error(`${key} 须为已声明的非负数量`);(merged as any)[key]=(av??0)+(bv??0)}
  legs.splice(index-1,2,merged);
 }else legs.splice(index===0?0:legs.length-1,1);
 candidate.route.points.splice(index,1);candidate.route.legs=legs;return candidate;
}

import {useEffect,useMemo,useRef,useState} from 'react';
import type {Dispatch,SetStateAction} from 'react';
import type {Cable,Layer,Project,TerrainSource} from './types';
import {request} from './api';
export type WorkspacePath={id:string;name:string;kind:string;project:Project};
export type WorkspaceAssembly={id:string;name:string;currency:string;items:any[];total_length_m:number};
export type WorkspaceAssociation={id:string;path_id:string;assembly_id:string;role:'deployment'|'alternative'};
export type Workspace={id:string;schema_version:2;name:string;currency:string;active_path_id:string;cable_types:Cable[];layers:Layer[];terrain_sources?:TerrainSource[];paths:WorkspacePath[];assemblies:WorkspaceAssembly[];associations:WorkspaceAssociation[];saved_revision?:number;[key:string]:any};
export type WorkspaceDocument={workspace:Workspace;draft:Project|null};
export type WorkspaceResult={workspace:Workspace;project:Project;analysis:any;report:any;warnings:any[]};
export function materialize(workspace:Workspace):Project{const path=workspace.paths.find(p=>p.id===workspace.active_path_id)||workspace.paths[0];return {...path.project,id:path.id,name:path.name,cable_types:workspace.cable_types,layers:workspace.layers,...(workspace.terrain_sources!==undefined?{terrain_sources:workspace.terrain_sources}:{}),workspace_context:{workspace_id:workspace.id,path_id:path.id,workspace_revision:workspace.saved_revision,...workspace.associations.find(a=>a.path_id===path.id)}};}
export default function useWorkspaceDocument(notify:(message:string,error?:boolean)=>void){
  const [document,setDocumentState]=useState<WorkspaceDocument|null>(null),[aggregate,setAggregate]=useState<any>(null),[pending,setPending]=useState(false),[relationError,setRelationError]=useState('');
  const documentRef=useRef(document);documentRef.current=document;const notifyRef=useRef(notify);notifyRef.current=notify;
  const project=useMemo(()=>document?(document.draft||materialize(document.workspace)):null,[document]);const projectRef=useRef(project);projectRef.current=project;
  const inflight=useRef<{document:WorkspaceDocument;promise:Promise<Workspace>}|null>(null);
  function publish(next:WorkspaceDocument|null){documentRef.current=next;setDocumentState(next)}
  const setDocument:Dispatch<SetStateAction<WorkspaceDocument|null>>=value=>{publish(typeof value==='function'?value(documentRef.current):value);setRelationError('');setAggregate(null)};
  const setProject:Dispatch<SetStateAction<Project|null>>=value=>{const current=documentRef.current;if(!current)return;const next=typeof value==='function'?value(projectRef.current):value;if(!next)return;const draft={...next,id:current.workspace.active_path_id};delete draft.saved_revision;publish({...current,draft});projectRef.current=draft;setRelationError('');setAggregate(null)};
  async function flush(policy='auto_exclusive',extra:Record<string,any>={}):Promise<Workspace>{const snapshot=documentRef.current;if(!snapshot)throw new Error('工作区尚未载入');if(!snapshot.draft)return snapshot.workspace;if(inflight.current?.document===snapshot)return inflight.current.promise;
    const task=(async()=>{setPending(true);try{const result=await request<WorkspaceResult>('/workspace/action',{workspace:snapshot.workspace,config:{action:'update_path',path_id:snapshot.workspace.active_path_id,project:snapshot.draft,assembly_policy:policy,update_shared:true,...extra}});if(documentRef.current===snapshot){publish({workspace:result.workspace,draft:null});setAggregate(result.analysis);setRelationError('');return result.workspace}return flush(policy,extra)}catch(e){if(documentRef.current===snapshot)setRelationError(e instanceof Error?e.message:String(e));throw e}finally{if(inflight.current?.document===snapshot){inflight.current=null;setPending(false)}}})();inflight.current={document:snapshot,promise:task};return task;
  }
  useEffect(()=>{if(!document?.draft)return;const timer=setTimeout(()=>{flush().catch(e=>{if(documentRef.current===document)notifyRef.current(e instanceof Error?e.message:String(e),true)})},250);return()=>clearTimeout(timer)},[document]);
  useEffect(()=>{if(!document||document.draft||aggregate)return;let cancelled=false;request<any>('/workspace/analyze',document.workspace).then(data=>{if(!cancelled&&documentRef.current===document)setAggregate(data)}).catch(e=>{if(!cancelled)notifyRef.current(String(e),true)});return()=>{cancelled=true}},[document,aggregate]);
  async function loadProject(p:Project){const result=await request<WorkspaceResult>('/workspace/migrate',{project:p});publish({workspace:result.workspace,draft:null});setAggregate(result.analysis);setRelationError('');return result.project}
  async function loadWorkspace(workspace:Workspace){const result=await request<any>('/workspace/analyze',workspace);publish({workspace,draft:null});setAggregate(result);setRelationError('');return materialize(workspace)}
  async function action(config:Record<string,any>){const accepted=await flush();const snapshot=documentRef.current;const result=await request<WorkspaceResult>('/workspace/action',{workspace:accepted,config});if(documentRef.current!==snapshot)throw new Error('工作区在计算期间已修改，请重新提交关系操作');publish({workspace:result.workspace,draft:null});setAggregate(result.analysis);setRelationError('');return result}
  async function analyze(){const accepted=await flush();const result=await request<any>('/workspace/analyze',accepted);if(documentRef.current?.workspace===accepted)setAggregate(result);return result}
  function saved(workspace:Workspace,accepted:Workspace){const current=documentRef.current;if(!current||current.workspace.id!==accepted.id)return false;const unchanged=current.workspace===accepted&&!current.draft;publish({...current,workspace:unchanged?workspace:{...current.workspace,saved_revision:workspace.saved_revision}});return unchanged}
  return {document,documentRef,workspace:document?.workspace||null,project,setProject,setDocument,aggregate,pending:pending||!!document?.draft,relationError,flush,loadProject,loadWorkspace,action,analyze,saved};
}

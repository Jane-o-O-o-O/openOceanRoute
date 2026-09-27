import type {Analysis,CircularArc,Project} from './types';
import type {Workspace,WorkspaceDocument} from './useWorkspaceDocument';

export type ArcMove={point_id:string;longitude:number;latitude:number};
export type ArcOption={start_point_id:string;end_point_id:string;radius_m?:number;branch?:'preserve'|'minor'|'major'|'left'|'right';full_circle_policy?:'reject_move'|'preserve_endpoint_center_bearing'};
export type ArcEditConfig={moves:ArcMove[];arc_options?:ArcOption[];max_arc_rebuilds?:number;max_work_units?:number;max_output_bytes?:number};
export type ArcChange={start_point_id:string;end_point_id:string;source_leg_indexes:number[];old_geometry:CircularArc;new_geometry:CircularArc;evidence:Record<string,any>;budget:Record<string,any>};
export type ArcJoin={point_id:string;before:{incoming_tangent_deg:number|null;outgoing_tangent_deg:number|null;turn_deg:number|null};after:{incoming_tangent_deg:number|null;outgoing_tangent_deg:number|null;turn_deg:number|null}};
export type ArcEditReport={operation:'arc_edit';config:ArcEditConfig;changed:boolean;moved_point_ids:string[];result_selection_point_id:string;arc_changes:ArcChange[];joins:ArcJoin[];generated_marker_ids:string[];before:{surface_length_m:number;point_count:number};after:{surface_length_m:number;point_count:number};manufacturing:Record<string,any>;constraint_reconciliation:Record<string,any>|null;profile_invalidation:Record<string,any>;side_slopes_invalidation:Record<string,any>;budget:Record<string,any>};
export type ArcEditPreview={workspace:Workspace;project:Project;analysis:{active_path_analysis:Analysis;[key:string]:unknown};report:Record<string,any>;tool_report:ArcEditReport;warnings:any[];operation:{kind:'arc_edit';path_id:string;config:ArcEditConfig};result_selection_point_id:string};
export type ArcEditSnapshot={document:WorkspaceDocument;contextKey:string;selected:string|null;selectionRevision:number};
export type ArcEditContext={document:WorkspaceDocument;contextKey:string;selected:string|null;selectionRevision:number;pending:boolean;onSelectPoint:(id:string)=>void;onApply:(candidate:ArcEditPreview,expected:ArcEditSnapshot,isCurrent:()=>boolean)=>Promise<boolean>};
export type ArcEditIntent={moves:ArcMove[];source:'map'|'rpl'|'projected';token:string};

export function arcEndpointIds(project:Project){return new Set(project.route.legs.flatMap((leg,i)=>leg.geometry?.type==='circular_arc'?[project.route.points[i]?.id,project.route.points[i+1]?.id].filter((id):id is string=>!!id):[]))}
export function movesTouchArc(project:Project,moves:ArcMove[]){const ids=arcEndpointIds(project);return moves.some(move=>ids.has(move.point_id)&&project.route.points.some(p=>p.id===move.point_id&&(!p.constraint||p.constraint==='rigid')))}

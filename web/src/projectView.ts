import type {Project} from './types';

/** UI view of valid core schemas. Never mutates the stored workspace or fills physical measurements. */
export function projectView(input:Project):Project{
 const p=structuredClone(input),raw=p as any;
 p.schema_version=p.schema_version??1;p.id=p.id||crypto.randomUUID();p.name=p.name||'海缆工程';p.crs=p.crs||'EPSG:4326';
 p.route={id:'route-1',name:'未命名路由',curve:'rhumb',mode:'flexible',slack_basis:'surface',slack_pct:0,legs:[],...raw.route};
 p.route.points=(p.route.points||[]).map((point,i)=>Object.assign({id:`p${i+1}`,label:'',depth_m:null,note:''},point));
 p.route.legs=p.route.legs||[];
 p.layers=p.layers||[];p.bodies=p.bodies||[];p.cable_types=p.cable_types||[];
 p.assembly_references=p.assembly_references||[];
 p.profile=Object.assign({samples:[],source:''},p.profile);
 // These administrative defaults match core.py; absent cable properties remain absent.
 p.costs=Object.assign({currency:'CNY',vessel_day_rate:0,burial_per_m:0,contingency_pct:0},p.costs);
 p.rules=Object.assign({max_slope_deg:15,min_slack_pct:0,max_slack_pct:5,corridor_m:0},p.rules);
 return p;
}

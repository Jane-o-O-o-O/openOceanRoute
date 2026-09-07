import {test,expect} from '@playwright/test';
import {readFile} from 'node:fs/promises';
import {apiURL} from './test-environment';
import {artifactPath} from './artifact-path';

async function ready(page:any){await expect(page.locator('.calculation-status')).toHaveText('工程计算已更新',{timeout:25000})}
async function load(page:any){
  await page.goto('/');await ready(page);await page.getByRole('button',{name:'敷设仿真',exact:true}).click();
  await page.locator('.solver-tabs button').filter({has:page.getByText('动态敷设',{exact:true})}).click();
  await page.locator('.equilibrium-initial-settings>summary').click();
  await page.getByRole('button',{name:'加载明确合成异质缆与零长度点实体初态',exact:true}).click();
}
async function actual(page:any,path:string,button:string){const response=page.waitForResponse((r:any)=>r.url().endsWith('/api/'+path)&&r.request().method()==='POST');await page.getByRole('button',{name:button,exact:true}).click();return await response}
async function num(page:any,label:string,value:number,exact=true){const input=page.getByLabel(label,{exact});await input.fill(String(value));await input.blur()}
async function materials(page:any){const details=page.locator('.mixed-materials');if(!await details.evaluate((e:HTMLDetailsElement)=>e.open))await details.locator('summary').click()}
const schema='oceanroute.dynamic.initial-equilibrium.provenance.v2';

test('actual heterogeneous point-buoy proof keeps signed loads, mixed segment compliance and inventory through browser checkpoint restoration',async({page})=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));await load(page);
  await expect(page.locator('.equilibrium-diagnostics')).toHaveCount(0);await expect(page.locator('.scene-host canvas')).toHaveCount(0);
  const response=await actual(page,'simulation/prepare-equilibrium-initial','预备并独立验证动态初态');expect(response.status()).toBe(200);
  const initial=await response.json(),proof=initial.provenance;
  expect(proof.schema).toBe(schema);expect(proof.verification.accepted).toBe(true);
  expect(proof.initial_material_length_m).toBe(27);expect(proof.initial_suspended_material_m).toBe(4);expect(proof.initial_paid_out_m).toBe(0);
  expect(proof.initial_snapshot.node_material_m).toEqual([31,19,17,14,13,9,7,4]);
  expect(proof.initial_snapshot.segment_ea_n[4]).toBeCloseTo(4/(1.2/10000+2.8/24000),8);
  expect(proof.material_loading.segment_cable_wet_weight_n[4]).toBeCloseTo(4*1.2+7*2.8,10);
  expect(proof.material_loading.segment_compliance_m_n[4]).toBeCloseTo(1.2/10000+2.8/24000,12);
  expect(proof.material_loading.signed_total_wet_weight_n).toBeCloseTo(120.4,9);
  expect(proof.material_loading.node_body_wet_weight_n[2]).toBeCloseTo(-50*1.3/3,10);
  expect(proof.initial_snapshot.node_wet_weight_n[2]).toBeLessThan(0);expect(proof.initial_snapshot.node_wet_weight_n[3]).toBeLessThan(0);
  const point=proof.material_loading.point_bodies[0];expect(point.wet_weight_n).toBe(-50);expect(point.length_m).toBe(0);expect(point.deployed_fraction).toBeCloseTo(1,12);
  expect(point.node_fractions[2]).toBeCloseTo(1.3/3,12);expect(point.node_fractions[3]).toBeCloseTo(1.7/3,12);
  await expect(page.locator('.equilibrium-initial-settings .equilibrium-diagnostics')).toContainText('材料 / 点负载证据 v2');
  const run=await actual(page,'simulation/dynamic','运行计算');expect(run.status()).toBe(200);const result=await run.json();
  expect(result.initialization.schema).toBe(schema);expect(result.checkpoint.schema_version).toBe(3);expect(result.checkpoint.model).toBe('material-lumped-mass-xpbd-cable-lay-v4');
  expect(result.frames[0].touchdown).toBeNull();expect(result.frames[0].bottom_tension_n).toBeNull();expect(result.frames[0].anchor_position_m).toEqual(initial.positions.at(-1));
  expect(result.frames[0].node_material_m[0]).toBe(31);expect(result.summary.paid_out_m).toBe(0);
  expect(result.frames.at(-1).inline_bodies[0].deployed_wet_weight_n).toBe(-50);
  const drift=Math.max(...result.frames.at(-1).nodes.flatMap((p:number[],i:number)=>p.map((v:number,j:number)=>Math.abs(v-result.frames[0].nodes[i][j]))));expect(drift).toBeLessThan(1e-6);
  const main=page.locator('.sim-main .equilibrium-diagnostics');await main.locator('.equilibrium-point-bodies>summary').click();
  await expect(main.locator('.equilibrium-point-bodies tbody tr').first().locator('td').nth(4)).toHaveText('-50');
  await main.locator('.equilibrium-material-segments>summary').click();await expect(main.locator('.equilibrium-material-segments tbody tr')).toHaveCount(7);
  await expect(page.locator('.scene-legend')).toContainText('无实际触地点');await expect(page.locator('.scene-legend')).toContainText('非刚体形状');
  await page.setViewportSize({width:1800,height:1600});
  await page.locator('.sim-main').evaluate((e:HTMLElement)=>e.scrollTop=0);
  await page.screenshot({path:artifactPath('heterogeneous-signed-point-load-proof.png'),fullPage:true});
  const downloaded=page.waitForEvent('download');await page.getByRole('button',{name:'保存末状态',exact:true}).click();const path=(await(await downloaded).path())!;
  const saved=JSON.parse(await readFile(path,'utf8'));expect(saved.state.initialization_provenance.material_loading).toEqual(JSON.parse(JSON.stringify(result.initialization.material_loading)));
  await page.locator('.checkpoint-settings input[type=file]').setInputFiles(path);
  await expect(page.locator('.checkpoint-settings textarea')).not.toHaveValue('');
  await expect(page.getByLabel('模拟时长 / s',{exact:true})).toHaveValue('10');
  await num(page,'模拟时长 / s',.08);
  const restored=await actual(page,'simulation/dynamic','运行计算');expect(restored.status()).toBe(200);const continued=await restored.json();
  expect(continued.frames[0].time_s).toBeCloseTo(.08,10);expect(continued.frames.at(-1).time_s).toBeCloseTo(.16,10);
  expect(continued.frames[0].nodes).toEqual(result.frames.at(-1).nodes);expect(continued.initialization.material_loading).toEqual(saved.state.initialization_provenance.material_loading);
  expect(continued.frames.at(-1).inline_bodies[0].deployed_wet_weight_n).toBe(-50);expect(continued.summary.paid_out_m).toBe(0);expect(errors).toEqual([]);
});

test('real late mixed-material preview is discarded and finite initial bodies, active EI, current and initialization budget failures expose no old proof or shape',async({page})=>{
  await load(page);let enter!:()=>void,release!:()=>void;const entered=new Promise<void>(resolve=>enter=resolve),gate=new Promise<void>(resolve=>release=resolve);
  await page.route('**/api/simulation/prepare-equilibrium-initial',async route=>{const response=await route.fetch();enter();await gate;await route.fulfill({response})});
  await page.getByRole('button',{name:'预备并独立验证动态初态',exact:true}).click();await entered;await materials(page);
  const rows=JSON.parse(await page.getByLabel('材料区段 JSON',{exact:true}).inputValue());rows[1].wet_weight_n_m=8;
  await page.getByLabel('材料区段 JSON',{exact:true}).fill(JSON.stringify(rows));release();await expect(page.getByRole('button',{name:'预备并独立验证动态初态',exact:true})).toBeEnabled();
  await expect(page.locator('.equilibrium-diagnostics')).toHaveCount(0);await page.unroute('**/api/simulation/prepare-equilibrium-initial');
  await page.getByRole('button',{name:'加载明确合成异质缆与零长度点实体初态',exact:true}).click();
  const valid=await actual(page,'simulation/dynamic','运行计算');expect(valid.status()).toBe(200);await expect(page.locator('.scene-host canvas')).toHaveCount(1);
  await materials(page);const bodies=JSON.parse(await page.getByLabel('在线缆体 JSON',{exact:true}).inputValue());bodies[0].length_m=1;
  await page.getByLabel('在线缆体 JSON',{exact:true}).fill(JSON.stringify(bodies));const finite=await actual(page,'simulation/prepare-equilibrium-initial','预备并独立验证动态初态');expect(finite.status()).toBe(422);
  await expect(page.locator('.equilibrium-initial-error')).toBeVisible();await expect(page.locator('.equilibrium-diagnostics')).toHaveCount(0);await expect(page.locator('.scene-host canvas')).toHaveCount(0);await expect(page.getByRole('button',{name:'下载结果',exact:true})).toBeDisabled();
  bodies[0].length_m=0;await page.getByLabel('在线缆体 JSON',{exact:true}).fill(JSON.stringify(bodies));rows[1].wet_weight_n_m=7;rows[1].ei_n_m2=1;
  await page.getByLabel('材料区段 JSON',{exact:true}).fill(JSON.stringify(rows));expect((await actual(page,'simulation/prepare-equilibrium-initial','预备并独立验证动态初态')).status()).toBe(422);
  rows[1].ei_n_m2=0;await page.getByLabel('材料区段 JSON',{exact:true}).fill(JSON.stringify(rows));await num(page,'X 向海流 / m·s⁻¹',.1);
  expect((await actual(page,'simulation/prepare-equilibrium-initial','预备并独立验证动态初态')).status()).toBe(422);await num(page,'X 向海流 / m·s⁻¹',0);
  const raw=JSON.parse(await page.getByLabel('明确动态定端初态 JSON',{exact:true}).inputValue());raw.solver={max_work_units:1};await page.getByLabel('明确动态定端初态 JSON',{exact:true}).fill(JSON.stringify(raw));
  expect((await actual(page,'simulation/prepare-equilibrium-initial','预备并独立验证动态初态')).status()).toBe(422);await expect(page.locator('.equilibrium-initial-error')).toBeVisible();
  await expect(page.locator('.equilibrium-diagnostics')).toHaveCount(0);await expect(page.locator('.scene-host canvas')).toHaveCount(0);await expect(page.getByRole('button',{name:'下载结果',exact:true})).toBeDisabled();
});

test('real geographic mixed-material window shows signed proof and preserves natural inventory in a durable partial job and child resume',async({page,request})=>{
  const example=JSON.parse(await readFile(new URL('../../examples/heterogeneous-initial-plan-voyage.json',import.meta.url),'utf8'));
  const project={...example.project,id:'heterogeneous-ui-'+Date.now(),name:'异质地理制造窗口 '+Date.now(),schema_version:1};
  const migrated=await request.post(apiURL+'/workspace/migrate',{data:{project}});expect(migrated.status()).toBe(200);const envelope=await migrated.json();envelope.workspace.name=project.name;
  const stored=await request.post(apiURL+'/workspaces',{data:envelope.workspace});expect(stored.status()).toBe(200);
  await page.goto('/');await ready(page);await page.getByRole('button',{name:'打开',exact:true}).click();await page.getByRole('button').filter({has:page.getByText(project.name,{exact:true})}).click();await ready(page);
  await page.getByRole('button',{name:'敷设仿真',exact:true}).click();await page.getByRole('button',{name:'持续计算 / 网格审查',exact:true}).click();await page.locator('.plan-voyage-preparation>summary').click();
  await page.getByLabel('预备窗口初态方法',{exact:true}).selectOption('equilibrium');await page.getByLabel('预备地理二维床格 JSON',{exact:true}).fill(JSON.stringify(example.config.seabed_grid));
  await page.getByLabel('预备地理定端初态 JSON',{exact:true}).fill(JSON.stringify(example.config.equilibrium_start));await num(page,'预备窗口时长 / s',.08);await num(page,'原计划开始时刻 / s',example.config.start_time_s,false);
  await num(page,'预备海床张力 / N',10);await num(page,'原计划采样站距 / m',30);await page.getByLabel('预备动力求解与环境参数 JSON',{exact:true}).fill(JSON.stringify(example.config.simulation));
  await page.getByLabel('预备任务预算与网格策略 JSON',{exact:true}).fill(JSON.stringify({...example.config.voyage,max_chunks:1,max_total_work_units:120000000,max_output_frames:256,max_mesh_records:64}));
  const response=await actual(page,'shipplan/prepare-voyage','预备并审查计划窗口');expect(response.status()).toBe(200);const prepared=await response.json(),proof=prepared.mapping.initial_equilibrium_preparation.provenance;
  expect(proof.schema).toBe(schema);expect(proof.material_loading.point_bodies[0].wet_weight_n).toBe(-50);expect(proof.initial_paid_out_m).toBe(0);
  expect(prepared.mapping.manufacturing_origin_m).toBeCloseTo(4,10);expect(prepared.mapping.initial_manufacturing_top_m).toBeCloseTo(31,10);expect(prepared.mapping.initial_natural_length_m).toBe(27);
  expect(prepared.mapping.terrain_frame.original_origin_projected_m).toEqual([11,-7]);
  const sent=JSON.parse(response.request().postData()!);expect(sent.project.cable_types[0].mass_kg_m).toBe(1.2);expect(sent.project.bodies[0].wet_weight_n).toBe(-50);
  await expect(page.locator('.plan-voyage-preparation .equilibrium-diagnostics')).toContainText('材料 / 点负载证据 v2');
  await page.getByRole('button',{name:'应用预备窗口到任务配置',exact:true}).click();const submitted=await actual(page,'voyage/jobs','提交后台持续计算');expect(submitted.status()).toBe(200);const job=await submitted.json();
  await expect(page.locator('.voyage-job-status>div:first-child>strong')).toHaveText('达到限制后停止',{timeout:30000});
  const first=await(await request.get(apiURL+'/voyage/jobs/'+job.id+'/result')).json();expect(first.summary.end_time_s).toBeCloseTo(.04,10);expect(first.frames[0].paid_out_m).toBe(0);expect(first.frames[0].touchdown).toBeNull();
  expect(first.checkpoint.physical_checkpoint.state.initialization_provenance.material_loading).toEqual(proof.material_loading);
  expect(first.frames[0].node_material_m[0]).toBeCloseTo(31,10);await expect(page.locator('.sim-main>.equilibrium-diagnostics')).toContainText('材料 / 点负载证据 v2');
  await num(page,'请求继续计算时长 / s',.04);await num(page,'本次分块数量上限',10);const resumed=await actual(page,'voyage/jobs/'+job.id+'/resume','按当前时长继续为子任务');expect(resumed.status()).toBe(200);const child=await resumed.json();
  await expect(page.locator('.voyage-job-status>div:first-child>strong')).toHaveText('已完成请求时段',{timeout:30000});const continued=await(await request.get(apiURL+'/voyage/jobs/'+child.id+'/result')).json();
  expect(continued.summary.end_time_s).toBeCloseTo(.08,10);expect(continued.frames[0].nodes).toEqual(first.frames.at(-1).nodes);expect(continued.summary.paid_out_m).toBeCloseTo(.51*.08,10);
  expect(continued.frames.at(-1).inline_bodies[0].deployed_wet_weight_n).toBe(-50);expect(continued.checkpoint.physical_checkpoint.state.initialization_provenance.material_loading).toEqual(proof.material_loading);
  expect(continued.plan_mapping).toEqual(first.plan_mapping);await page.locator('.plan-voyage-preparation>summary').click();await page.locator('.voyage-layout .sim-view').scrollIntoViewIfNeeded();
  await page.screenshot({path:artifactPath('heterogeneous-geographic-inventory-job.png'),fullPage:true});
});

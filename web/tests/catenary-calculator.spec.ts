import {test,expect,type Page} from '@playwright/test';
import {readFile} from 'node:fs/promises';
import {apiURL} from './test-environment';
import {artifactPath} from './artifact-path';

async function open(page:Page){
 await page.goto('/');
 await expect(page.locator('.calculation-status')).toHaveText('工程计算已更新',{timeout:20000});
 await page.getByRole('button',{name:'敷设仿真',exact:true}).click();
 await page.getByRole('button',{name:'悬链线 Calculator',exact:true}).click();
 await expect(page.locator('.catenary-calculator-panel')).toBeVisible();
 await page.locator('.calculator-examples summary').click();
}
async function number(page:Page,label:string,value:number){const input=page.getByLabel(label,{exact:true});await input.fill(String(value));await input.blur()}
async function solve(page:Page,status=200){
 const response=page.waitForResponse(r=>r.url().endsWith('/api/simulation/catenary-calculator'));
 await page.getByRole('button',{name:'运行四边界换算',exact:true}).click();
 const actual=await response;expect(actual.status()).toBe(status);return actual.json();
}
async function downloaded(page:Page,name:string){
 const event=page.waitForEvent('download');await page.getByRole('button',{name,exact:true}).click();
 const file=await event;return {file,data:JSON.parse(await readFile((await file.path())!,'utf8'))};
}
async function shot(page:Page,name:string){
 if(await page.getByRole('button',{name:'关闭提示',exact:true}).isVisible())await page.getByRole('button',{name:'关闭提示',exact:true}).click();
 await page.locator('.sim-main').evaluate(e=>e.scrollTop=0);
 await page.screenshot({path:artifactPath(name+'.png'),fullPage:true});
}
function equalShape(first:any,second:any){
 expect(second.accepted).toBe(true);expect(second.selected).not.toBeNull();
 const a=first.selected.result,b=second.selected.result;
 for(const key of ['bottom_tension_n','top_tension_n','natural_length_m','stretched_arc_length_m','top_angle_from_horizontal_deg','layback_m'])expect(b.summary[key]).toBeCloseTo(a.summary[key],6);
 expect(b.nodes).toHaveLength(a.nodes.length);
 a.nodes.forEach((p:number[],i:number)=>p.forEach((v,j)=>expect(b.nodes[i][j]).toBeCloseTo(v,6)));
}

test('four boundary conversions use actual unrounded roots and explicit natural versus stretched cable lengths',async({page,request})=>{
 const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));await open(page);
 await expect(page.getByRole('button',{name:'运行四边界换算',exact:true})).toBeDisabled();
 await page.getByRole('button',{name:'载入 2000 m 平床不可伸长例',exact:true}).click();
 const first=await solve(page);expect(first.accepted).toBe(true);expect(first.validation_status).toBe('research');
 expect(first.selected.result.summary.bottom_tension_n).toBe(34000);expect(first.selected.result.summary.top_tension_n).toBeCloseTo(68000,7);
 expect(first.selected.result.summary.top_angle_from_horizontal_deg).toBeCloseTo(60,8);
 expect(first.selected.result.summary.natural_length_m).toBeCloseTo(3464.1016151378,7);
 await expect(page.locator('.calculator-status.accepted')).toBeVisible();await expect(page.locator('.scene-host canvas')).toBeVisible();
 await page.getByRole('button',{name:'用结果顶部张力作为输入',exact:true}).click();
 await expect(page.locator('.scene-host canvas')).toHaveCount(0);equalShape(first,await solve(page));
 await page.getByRole('button',{name:'用结果水平角作为输入',exact:true}).click();
 const angle=await solve(page);expect(angle.boundary.reference).toBe('horizontal');expect(angle.boundary.direction).toBe('touchdown_to_vessel');equalShape(first,angle);
 await page.getByRole('button',{name:'用结果自然长作为输入',exact:true}).click();
 const length=await solve(page);expect(length.boundary.length_basis).toBe('natural');equalShape(first,length);
 await page.getByRole('button',{name:'用结果伸长弧长作为输入',exact:true}).click();
 const arc=await solve(page);expect(arc.boundary.length_basis).toBe('stretched_arc');equalShape(first,arc);
 await page.getByRole('button',{name:'载入有限 EA 坡床例',exact:true}).click();
 const finite=await solve(page);expect(finite.accepted).toBe(true);expect(finite.selected.result.summary.natural_length_m).toBeCloseTo(50.3803848342,7);
 expect(finite.selected.result.summary.stretched_arc_length_m).toBeGreaterThan(finite.selected.result.summary.natural_length_m);
 await page.getByRole('button',{name:'用结果自然长作为输入',exact:true}).click();equalShape(finite,await solve(page));
 await page.getByRole('button',{name:'用结果伸长弧长作为输入',exact:true}).click();const elasticArc=await solve(page);equalShape(finite,elasticArc);
 const proof=await request.post(apiURL+'/simulation/catenary-calculator',{data:{config:{boundary:elasticArc.boundary,seabed_grid:elasticArc.selected.result.seabed.grid,wet_weight_n_m:4,ea_n:100000,vessel_position_m:[0,0,0],heading_deg:90,nodes:41}}});expect(proof.ok()).toBe(true);equalShape(finite,await proof.json());
 await expect(page.getByRole('img',{name:'选定 Calculator 根的实际节点与床高剖面',exact:true})).toBeVisible();
 await page.locator('.calculator-force-nodes summary').click();await expect(page.locator('.calculator-force-nodes tbody tr')).toHaveCount(41);
 const saved=await downloaded(page,'下载 Calculator 结果');expect(saved.data.selected.result.nodes).toEqual(elasticArc.selected.result.nodes);expect(saved.data.checkpoint).toBeUndefined();
 await shot(page,'calculator-explicit-elastic-length');
 await page.getByLabel('单位自然长度湿重 / N·m⁻¹',{exact:true}).fill('4.1');
 await expect(page.locator('.calculator-stale')).toBeVisible();await expect(page.locator('.scene-host canvas')).toHaveCount(0);
 await expect(page.getByRole('button',{name:'下载 Calculator 结果',exact:true})).toBeDisabled();expect(errors).toEqual([]);
});

test('downhill double roots require explicit branch selection and a delayed real response cannot revive a changed input',async({page})=>{
 const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));await open(page);
 await page.getByRole('button',{name:'载入下坡双根例',exact:true}).click();const ambiguous=await solve(page);
 expect(ambiguous.accepted).toBe(false);expect(ambiguous.selected).toBeNull();expect(ambiguous.solver.root_enumeration_complete).toBe(true);
 expect(ambiguous.solver.mathematical_root_count).toBe(2);expect(ambiguous.solver.usable_root_count).toBe(2);
 expect(ambiguous.candidates.every((c:any)=>c.accepted)).toBe(true);await expect(page.locator('.calculator-candidates tbody tr')).toHaveCount(2);
 await expect(page.locator('.calculator-status.rejected')).toContainText('没有已选定可用解');await expect(page.locator('.scene-host canvas')).toHaveCount(0);
 await shot(page,'calculator-unselected-two-roots');
 await page.getByRole('button',{name:'切换为低 B 根策略',exact:true}).click();await expect(page.locator('.calculator-stale')).toBeVisible();
 const low=await solve(page);expect(low.accepted).toBe(true);expect(low.selected.bottom_tension_n).toBeCloseTo(4.003975,5);
 await page.getByLabel('Calculator 多根处理',{exact:true}).selectOption('highest_bottom_tension');
 const high=await solve(page);expect(high.accepted).toBe(true);expect(high.selected.bottom_tension_n).toBeGreaterThan(low.selected.bottom_tension_n);
 expect(high.selected.result.summary.natural_length_m).toBeCloseTo(28,8);await expect(page.locator('.scene-host canvas')).toBeVisible();
 await shot(page,'calculator-explicit-high-root');
 await page.getByLabel('Calculator 多根处理',{exact:true}).selectOption('enumerate');
 const enumeration=await solve(page);expect(enumeration.selected).toBeNull();expect(enumeration.candidates).toHaveLength(2);await expect(page.locator('.scene-host canvas')).toHaveCount(0);
 let release!:()=>void,forwardReady!:()=>void;const gate=new Promise<void>(r=>release=r),ready=new Promise<void>(r=>forwardReady=r);
 await page.route('**/api/simulation/catenary-calculator',async route=>{const real=await route.fetch();expect(real.status()).toBe(200);forwardReady();await gate;await route.fulfill({response:real})});
 const finished=page.waitForResponse(r=>r.url().endsWith('/api/simulation/catenary-calculator'));
 await page.getByRole('button',{name:'运行四边界换算',exact:true}).click();await ready;
 await page.getByLabel('单位自然长度湿重 / N·m⁻¹',{exact:true}).fill('4.2');release();await finished;
 await expect(page.getByRole('button',{name:'运行四边界换算',exact:true})).toBeVisible();await expect(page.locator('.calculator-status')).toHaveCount(0);
 await expect(page.locator('.scene-host canvas')).toHaveCount(0);await expect(page.getByRole('button',{name:'下载未验收 Calculator 诊断',exact:true})).toBeDisabled();
 await page.unroute('**/api/simulation/catenary-calculator');expect(errors).toEqual([]);
});

test('actual finite-grid coverage rejection never substitutes a lower root and hard budget failure removes old geometry',async({page})=>{
 const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));await open(page);
 await page.getByRole('button',{name:'载入下坡双根例',exact:true}).click();
 await page.getByLabel('Calculator 多根处理',{exact:true}).selectOption('highest_bottom_tension');expect((await solve(page)).accepted).toBe(true);
 await number(page,'床格 X 最小值 / m',-10);await number(page,'床格 X 最大值 / m',10);
 await expect(page.locator('.calculator-grid-stale')).toBeVisible();await expect(page.locator('.scene-host canvas')).toHaveCount(0);
 await expect(page.getByRole('button',{name:'运行四边界换算',exact:true})).toBeDisabled();await page.getByRole('button',{name:'明确生成有限仿射床格',exact:true}).click();
 const rejected=await solve(page);expect(rejected.accepted).toBe(false);expect(rejected.selected).toBeNull();
 expect(rejected.solver.mathematical_root_count).toBe(2);expect(rejected.solver.usable_root_count).toBe(1);
 expect(rejected.candidates.filter((c:any)=>!c.accepted)).toHaveLength(1);expect(rejected.candidates.find((c:any)=>!c.accepted).rejection_codes.length).toBeGreaterThan(0);
 await expect(page.locator('.scene-host canvas')).toHaveCount(0);await expect(page.locator('.calculator-candidates tbody tr.invalid')).toHaveCount(1);
 const diagnostic=await downloaded(page,'下载未验收 Calculator 诊断');expect(diagnostic.file.suggestedFilename()).toContain('NOT-ACCEPTED');expect(diagnostic.data.selected).toBeNull();
 await page.getByLabel('Calculator 多根处理',{exact:true}).selectOption('require_unique');const stillAmbiguous=await solve(page);expect(stillAmbiguous.selected).toBeNull();expect(stillAmbiguous.accepted).toBe(false);
 await page.getByLabel('Calculator 多根处理',{exact:true}).selectOption('lowest_bottom_tension');expect((await solve(page)).accepted).toBe(true);await expect(page.locator('.scene-host canvas')).toBeVisible();
 await page.getByText('真实求解预算与长度边界',{exact:true}).click();await number(page,'Calculator 总工作量上限',1);
 await expect(page.locator('.scene-host canvas')).toHaveCount(0);await solve(page,422);
 await expect(page.locator('.calculator-error')).toBeVisible();await expect(page.locator('.calculator-status')).toHaveCount(0);await expect(page.locator('.scene-host canvas')).toHaveCount(0);
 await expect(page.getByRole('button',{name:'下载未验收 Calculator 诊断',exact:true})).toBeDisabled();
 // This independently integrated stretched-arc fold is unresolved at its
 // floating-point peak; the main UI must never call its unknown root count 0.
 await page.getByRole('button',{name:'载入下坡双根例',exact:true}).click();
 await page.getByLabel('Calculator 轴向伸长模型',{exact:true}).selectOption('finite');
 await number(page,'Calculator 轴向刚度 EA / N',10000);
 await page.getByLabel('Calculator 缆长定义',{exact:true}).selectOption('stretched_arc');
 await number(page,'船位床面水深 / m',32.172032663085375);
 await number(page,'床格 X 最小值 / m',-31);await number(page,'床格 X 最大值 / m',80);
 await page.getByRole('button',{name:'明确生成有限仿射床格',exact:true}).click();
 const unresolved=await solve(page);expect(unresolved.accepted).toBe(false);expect(unresolved.selected).toBeNull();
 expect(unresolved.solver.root_enumeration_complete).toBe(false);
 expect(unresolved.solver.failure).toContain('peak');expect(unresolved.solver.failure_diagnostic.possible_root_counts).toEqual([0,1,2]);
 await expect(page.locator('.calculator-status.rejected')).toContainText('根枚举未完成，不能选择');
 await expect(page.locator('.calculator-status')).toContainText('数学根 未分辨');
 await expect(page.locator('.calculator-status')).not.toContainText('数学根 0');
 await expect(page.locator('.calculator-enumeration-failure')).toContainText(unresolved.solver.failure);
 await expect(page.locator('.calculator-enumeration-failure')).toContainText('0 / 1 / 2');
 await expect(page.locator('.scene-host canvas')).toHaveCount(0);
 await shot(page,'calculator-peak-unresolved');expect(errors).toEqual([]);
});

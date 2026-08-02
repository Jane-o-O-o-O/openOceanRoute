import {test,expect} from '@playwright/test';
async function ready(page:any){await expect(page.locator('.calculation-status')).toHaveText('工程计算已更新',{timeout:20000})}
async function tool(page:any,name:string){await page.locator('.engineering-menu button').filter({has:page.getByText(name,{exact:true})}).click()}
async function compute(page:any){await page.getByRole('button',{name:'计算结果',exact:true}).click();await expect(page.locator('.tool-result')).toBeVisible({timeout:25000})}
test('subdivision, depth bands, templates and geodetic tool apply real results',async({page})=>{
  const errors:string[]=[];page.on('pageerror',err=>errors.push(err.message));await page.goto('/');await ready(page);await page.getByRole('button',{name:'工程工具',exact:true}).click();
  const length=await page.locator('.metric').nth(2).locator('strong').innerText();await compute(page);await page.getByRole('button',{name:'应用到工作空间',exact:true}).click();await ready(page);
  await expect(page.locator('.metric').nth(2).locator('strong')).toHaveText(length);expect(await page.locator('.tree-points button').count()).toBeGreaterThan(10);
  await tool(page,'按水深分配缆型');await compute(page);await page.getByRole('button',{name:'应用到工作空间',exact:true}).click();await ready(page);await expect(page.locator('.metric').nth(1).locator('strong')).not.toHaveText('—');
  await tool(page,'余缆模板');await compute(page);await page.getByRole('button',{name:'应用到工作空间',exact:true}).click();await ready(page);
  await tool(page,'大地计算器');await compute(page);await expect(page.locator('.geodetic-destination')).toBeVisible();
  await page.screenshot({path:'artifacts/engineering-tools.png',fullPage:true});expect(errors).toEqual([]);
});
test('split saves two source projects and merge restores material balance',async({page,request})=>{
  await page.goto('/');await ready(page);const cable=await page.locator('.metric').nth(2).locator('strong').innerText();await page.getByRole('button',{name:'工程工具',exact:true}).click();await tool(page,'工程拆分');
  const response=page.waitForResponse((r:any)=>r.url().endsWith('/api/tools/split'));await compute(page);const split=await (await response).json();await expect(page.locator('.variant-card')).toHaveCount(2);await page.getByRole('button',{name:'保存两条子工程',exact:true}).click();await expect(page.getByText('已保存 2 条拆分工程',{exact:true})).toBeVisible();
  const sources=[];for(const p of split.projects){sources.push(await(await request.get('http://127.0.0.1:8765/api/projects/'+p.id)).json())}
  await tool(page,'工程合并');await page.getByLabel('当前工作空间作为第一条路线').uncheck();
  for(const p of sources){await page.locator('.merge-sources>div').first().getByRole('button').filter({has:page.getByText(p.name,{exact:true})}).first().click()}
  await compute(page);await page.getByRole('button',{name:'应用到工作空间',exact:true}).click();await ready(page);await expect(page.locator('.metric').nth(2).locator('strong')).toHaveText(cable);
});
test('ship plan, actual scenario comparison, target search and downloads work',async({page})=>{
  const errors:string[]=[];page.on('pageerror',err=>errors.push(err.message));await page.goto('/');await ready(page);await page.getByRole('button',{name:'敷设仿真',exact:true}).click();await page.getByRole('button',{name:'Ship Plan / LookAhead / 张力搜索',exact:true}).click();
  await page.getByRole('button',{name:'计算船舶工程',exact:true}).click();await expect(page.locator('.ship-instructions tbody tr').first()).toBeVisible({timeout:30000});expect(await page.locator('.ship-instructions tbody tr').count()).toBeGreaterThan(10);await expect(page.locator('.ship-map .leaflet-host')).toBeVisible();
  const download=page.waitForEvent('download');await page.getByRole('button',{name:'指令 CSV',exact:true}).click();expect((await download).suggestedFilename()).toContain('ship-plan.csv');await page.locator('.sim-main').evaluate((e:HTMLElement)=>e.scrollTop=0);await page.screenshot({path:'artifacts/ship-plan.png',fullPage:true});
  await page.locator('.ship-planning .solver-tabs button').filter({has:page.getByText('前瞻方案比较',{exact:true})}).click();await page.getByRole('button',{name:'计算船舶工程',exact:true}).click();await expect(page.locator('.comparison-chart>button')).toHaveCount(3,{timeout:30000});await expect(page.locator('.scene-host canvas')).toBeVisible();await page.locator('.comparison-chart>button').nth(1).click();await expect(page.locator('.sim-overlay>span')).toHaveText('增加放缆');await page.locator('.sim-main').evaluate((e:HTMLElement)=>e.scrollTop=0);await page.screenshot({path:'artifacts/look-ahead.png',fullPage:true});
  await page.locator('.ship-planning .solver-tabs button').filter({has:page.getByText('目标张力搜索',{exact:true})}).click();await page.getByLabel('局部模拟时长 / s',{exact:true}).fill('3');await page.getByLabel('局部模拟时长 / s',{exact:true}).blur();await page.getByLabel('评价时间窗 / s',{exact:true}).fill('1');await page.getByLabel('评价时间窗 / s',{exact:true}).blur();await page.getByLabel('最大求解次数',{exact:true}).fill('3');await page.getByLabel('最大求解次数',{exact:true}).blur();
  await page.getByRole('button',{name:'计算船舶工程',exact:true}).click();await expect(page.locator('.optimization-result')).toBeVisible({timeout:30000});expect(await page.locator('.ship-planning .table-wrap tbody tr').count()).toBeGreaterThanOrEqual(3);await page.locator('.sim-main').evaluate((e:HTMLElement)=>e.scrollTop=0);await page.screenshot({path:'artifacts/tension-search.png',fullPage:true});expect(errors).toEqual([]);
});

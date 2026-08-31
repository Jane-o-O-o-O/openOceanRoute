import {expect, test, type APIRequestContext, type Page} from '@playwright/test';
import {readFile} from 'node:fs/promises';
import {apiURL} from './test-environment';

// Every held response first goes to the actual API. No status, physical frame
// or checkpoint is manufactured by a route handler.
const terminal = ['completed', 'stopped', 'cancelled', 'failed', 'interrupted'];
const simulation = {
  depth_m:10, wet_weight_n_m:4, bottom_tension_n:10, nodes:16,
  ship_speed_m_s:0, payout_m_s:0, heading_deg:90,
  current_x_m_s:0, current_y_m_s:0, diameter_m:.02,
  mass_kg_m:.7298997321841252, ea_n:1e5, ei_n_m2:0,
  internal_dt_s:.01, dt_s:.02, solver_iterations:24,
};

async function create(request:APIRequestContext, seconds:number, running=false) {
  const response=await request.post(apiURL+'/voyage/jobs', {data:{project:{}, config:{
    duration_s:seconds, chunk_duration_s:running?.5:.04,
    max_chunks:running?10000:16, max_total_work_units:2000000000,
    max_output_frames:64, adaptive_mesh:{enabled:false},
    simulation:{...simulation,...(running?{internal_dt_s:.005,dt_s:.1}:{}),
      ...(seconds===.2?{bottom_tension_n:20}:{}),},
  }}});
  expect(response.status()).toBe(200);
  return await response.json();
}
async function ended(request:APIRequestContext,id:string) {
  await expect.poll(async()=>{
    const response=await request.get(apiURL+'/voyage/jobs/'+id);
    expect(response.status()).toBe(200);
    return terminal.includes((await response.json()).status);
  },{timeout:30000}).toBe(true);
  const response=await request.get(apiURL+'/voyage/jobs/'+id+'/result');
  expect(response.status()).toBe(200);
  return await response.json();
}
async function open(page:Page) {
  await page.goto('/');
  await expect(page.locator('.calculation-status')).toHaveText('工程计算已更新',{timeout:25000});
  await page.getByRole('button',{name:'敷设仿真',exact:true}).click();
  await page.getByRole('button',{name:'持续计算 / 网格审查',exact:true}).click();
}
async function choose(page:Page,id:string) {
  await page.getByRole('button',{name:'刷新任务列表',exact:true}).click();
  await page.locator('.voyage-job-list button').filter({has:page.getByText(id.slice(0,8),{exact:true})}).click();
  await expect(page.locator('.voyage-job-status')).toContainText(id);
}
async function resultVisible(page:Page,id:string) {
  await choose(page,id);
  await expect(page.locator('.voyage-chunks tbody tr').first()).toBeVisible();
}
function gate() {
  let enter!:()=>void,release!:()=>void;
  return {entered:new Promise<void>(resolve=>{enter=resolve}),
    released:new Promise<void>(resolve=>{release=resolve}),
    enter:()=>enter(),release:()=>release()};
}
async function downloadedResult(page:Page) {
  const pending=page.waitForEvent('download');
  await page.getByRole('button',{name:'下载真实分块结果',exact:true}).click();
  return JSON.parse(await readFile((await(await pending).path())!,'utf8'));
}
async function cleanup(request:APIRequestContext,ids:string[]) {
  for (const id of ids) {
    const response=await request.get(apiURL+'/voyage/jobs/'+id);
    if(response.status()!==200)continue;
    if(!terminal.includes((await response.json()).status)) {
      await request.post(apiURL+'/voyage/jobs/'+id+'/cancel',{data:{}});
      await expect.poll(async()=>terminal.includes((await(await request.get(apiURL+'/voyage/jobs/'+id)).json()).status),{timeout:30000}).toBe(true);
    }
    expect((await request.delete(apiURL+'/voyage/jobs/'+id)).status()).toBe(200);
  }
}

test('late actual cancel response cannot relabel another selected job or its physical evidence',async({page,request})=>{
  const ids:string[]=[],held=gate();
  try {
    const b=await create(request,.2);ids.push(b.id);
    const expected=await ended(request,b.id);
    await open(page);
    const a=await create(request,1000,true);ids.push(a.id);
    await choose(page,a.id);
    const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/api/voyage/jobs/'+a.id+'/cancel',async route=>{
      const actual=await route.fetch();expect(actual.status()).toBe(200);
      held.enter();await held.released;await route.fulfill({response:actual});
    });
    await page.getByRole('button',{name:'请求取消',exact:true}).click();
    await held.entered;
    await resultVisible(page,b.id);
    const response=page.waitForResponse(r=>r.url().endsWith('/api/voyage/jobs/'+a.id+'/cancel'));
    held.release();await(await response).finished();
    await expect(page.getByRole('button',{name:'提交后台持续计算',exact:true})).toBeEnabled();
    await expect(page.locator('.voyage-job-status')).toContainText(b.id);
    await expect(page.locator('.voyage-job-status')).not.toContainText(a.id);
    expect(await downloadedResult(page)).toEqual(expected);
    expect(errors).toEqual([]);
  } finally {held.release();await cleanup(request,ids);}
});

test('late actual child-resume response preserves the newly selected parent evidence',async({page,request})=>{
  const ids:string[]=[],held=gate();
  try {
    const a=await create(request,.12);ids.push(a.id);await ended(request,a.id);
    const b=await create(request,.2);ids.push(b.id);const expected=await ended(request,b.id);
    await open(page);await resultVisible(page,a.id);
    await page.getByLabel('请求继续计算时长 / s',{exact:true}).fill('.04');
    await page.getByLabel('请求继续计算时长 / s',{exact:true}).blur();
    await page.route('**/api/voyage/jobs/'+a.id+'/resume',async route=>{
      const actual=await route.fetch();expect(actual.status()).toBe(200);
      const child=await actual.json();ids.push(child.id);expect(child.parent_job_id).toBe(a.id);
      held.enter();await held.released;await route.fulfill({response:actual});
    });
    await page.getByRole('button',{name:'按当前时长继续为子任务',exact:true}).click();
    await held.entered;await resultVisible(page,b.id);
    const response=page.waitForResponse(r=>r.url().endsWith('/api/voyage/jobs/'+a.id+'/resume'));
    held.release();await(await response).finished();
    await expect(page.getByRole('button',{name:'提交后台持续计算',exact:true})).toBeEnabled();
    await expect(page.locator('.voyage-job-status')).toContainText(b.id);
    expect(await downloadedResult(page)).toEqual(expected);
  } finally {held.release();await cleanup(request,ids.reverse());}
});

test('late actual checkpoint fetch does not silently download the previously selected job',async({page,request})=>{
  const ids:string[]=[],held=gate();
  try {
    const a=await create(request,.12);ids.push(a.id);await ended(request,a.id);
    const b=await create(request,.2);ids.push(b.id);const expected=await ended(request,b.id);
    await open(page);await resultVisible(page,a.id);
    let downloads=0;page.on('download',()=>{downloads++});
    await page.route('**/api/voyage/jobs/'+a.id+'/checkpoint',async route=>{
      const actual=await route.fetch();expect(actual.status()).toBe(200);
      expect((await actual.json()).physical_checkpoint.time_s).toBeCloseTo(.12,10);
      held.enter();await held.released;await route.fulfill({response:actual});
    });
    await page.getByRole('button',{name:'保存最近完整断点',exact:true}).click();
    await held.entered;await resultVisible(page,b.id);
    const response=page.waitForResponse(r=>r.url().endsWith('/api/voyage/jobs/'+a.id+'/checkpoint'));
    held.release();await(await response).finished();
    // Observe completed browser render turns, not a fake time/HTTP response.
    await page.evaluate(()=>new Promise<void>(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve()))));
    expect(downloads).toBe(0);
    await expect(page.locator('.voyage-job-status')).toContainText(b.id);
    const pending=page.waitForEvent('download');
    await page.getByRole('button',{name:'保存最近完整断点',exact:true}).click();
    const download=await pending;
    expect(download.suggestedFilename()).toContain(b.id);
    expect(JSON.parse(await readFile((await download.path())!,'utf8'))).toEqual(expected.checkpoint);
  } finally {held.release();await cleanup(request,ids);}
});

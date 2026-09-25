// 用隔离 DOM 和可控制的异步响应验证续传预览与实际执行的一致性。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const page = fs.readFileSync('command_center/static/index.html', 'utf8');
const names = ['previewTrainingTask', 'renderTrainingTasks', 'executeCommand', 'loadTrainingTaskLog'];
const source = names.map(name => {
  const match = new RegExp(`^  (?:async )?function ${name}\\(`, 'm').exec(page);
  assert.ok(match, name);
  const rest = page.slice(match.index + 1);
  const next = /\n  (?:async )?function /.exec(rest);
  return rest.slice(0, next ? next.index : undefined);
}).join('\n');

function harness() {
  const elements = new Map();
  const pending = [];
  const context = {
    state: {form: {variables: {}}, training: {tasks: [{
      task_id: 'a', strategy: 'demo.A', updated_at: '2026-09-23T10:00:00',
      metrics: ['return'], resumable: true, trial_counts: {},
    }], resumeSelected: 'a'}},
    commandRequestSeq: 0, logRequestSeq: 0, refreshTimer: null, activeRun: '',
    toast: () => {},
    clearTimeout, setTimeout, console,
    $: id => {
      if (!elements.has(id)) elements.set(id, {value: '', textContent: '', innerHTML: '', disabled: false});
      return elements.get(id);
    },
    t: value => value, esc: value => String(value),
    document: {querySelectorAll: () => []},
    api: (url, options) => new Promise((resolve, reject) => pending.push({url, options, resolve, reject})),
    resumePayload: taskId => ({train_resume: taskId}),
    commandPayload: () => ({strategy: 'demo.Form'}),
    refreshCommand: () => {
      context.state.resumePreview = null;
      context.state.command = null;
      context.$('command-preview').value = '';
    },
    resetFollowTail: () => {}, setFollowText: () => {}, loadTrainingTasks: () => {},
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return {context, pending};
}

test('续传预览未返回时不能执行旧预览对应的命令', async () => {
  const {context, pending} = harness();
  context.$('command-preview').value = 'python run.py demo.Old';
  const request = context.previewTrainingTask('a');
  assert.equal(context.$('execute-command').disabled, true);
  assert.equal(context.$('resume-training').disabled, true);
  assert.equal(context.$('command-preview').value.includes('demo.Old'), false);
  await context.executeCommand('a');
  assert.equal(pending.length, 1);
  pending[0].resolve({display_command: 'python run.py demo.A'});
  await request;
  assert.equal(context.$('execute-command').disabled, false);
  assert.equal(context.$('resume-training').disabled, false);
  assert.equal(context.$('command-preview').value, 'python run.py demo.A');
});

test('过期的预览响应不能覆盖更新的任务选择', async () => {
  const {context, pending} = harness();
  context.state.training.tasks.push({...context.state.training.tasks[0], task_id: 'b', strategy: 'demo.B'});
  const first = context.previewTrainingTask('a');
  context.state.training.resumeSelected = 'b';
  const second = context.previewTrainingTask('b');
  pending[1].resolve({display_command: 'python run.py demo.B'});
  await second;
  pending[0].resolve({display_command: 'python run.py demo.A'});
  await first;
  assert.equal(context.state.resumePreview, 'b');
  assert.equal(context.$('command-preview').value, 'python run.py demo.B');
  assert.equal(context.$('execute-command').disabled, false);
});

test('续传预览失败时禁用执行，重新预览成功后恢复', async () => {
  const {context, pending} = harness();
  const first = context.previewTrainingTask('a');
  pending[0].reject(new Error('task unavailable'));
  await first;
  assert.equal(context.$('execute-command').disabled, true);
  assert.equal(context.$('resume-training').disabled, true);
  const second = context.previewTrainingTask('a');
  pending[1].resolve({display_command: 'python run.py demo.A'});
  await second;
  assert.equal(context.$('execute-command').disabled, false);
});

test('旧配置口径的隔离提示随恢复命令一起显示', async () => {
  const {context, pending} = harness();
  const request = context.previewTrainingTask('a');
  pending[0].resolve({display_command: 'python run.py demo.A', warnings: ['Historical scores are isolated.']});
  await request;
  assert.ok(context.$('warnings').innerHTML.includes('Historical scores are isolated.'));
});

test('任务分页每次最多十项并展示总数和页码', () => {
  const {context} = harness();
  const template = context.state.training.tasks[0];
  context.state.training.tasks = Array.from({length: 23}, (_, index) => ({...template, task_id: `task-${index}`, strategy: `demo.Task${index}`}));
  context.renderTrainingTasks();
  let html = context.$('training-tasks').innerHTML;
  assert.equal((html.match(/data-resume-task=/g) || []).length, 10);
  assert.ok(html.includes('23 tasks') && html.includes('page 1/3'));
  assert.ok(!html.includes('demo.Task10'));
  context.state.training.taskPage = 2;
  context.renderTrainingTasks();
  html = context.$('training-tasks').innerHTML;
  assert.equal((html.match(/data-resume-task=/g) || []).length, 3);
  assert.ok(html.includes('page 3/3') && html.includes('demo.Task22'));
});

test('选择后显示原始命令及最新行情命令以便确认和复制', async () => {
  const {context, pending} = harness();
  const request = context.previewTrainingTask('a');
  pending[0].resolve({display_command: 'resume', original_command: 'original', fresh_command: 'fresh --refresh', original_exact: true});
  await request;
  assert.equal(context.$('training-original-command').value, 'original');
  assert.equal(context.$('training-fresh-command').value, 'fresh --refresh');
  assert.equal(context.$('training-task-detail').hidden, false);
  assert.equal(pending.length, 1);
});

test('运行期间选择其它任务不会悄悄切换底部执行目标', async () => {
  const {context, pending} = harness();
  context.state.executing = true;
  context.state.resumePreview = 'a';
  context.$('execute-command').disabled = true;
  context.$('command-preview').value = 'python run.py demo.A';
  const request = context.previewTrainingTask('b');
  for (const item of pending) item.resolve({display_command: 'python run.py demo.B'});
  await request;
  assert.equal(context.state.resumePreview, 'a');
  assert.equal(context.$('command-preview').value, 'python run.py demo.A');
});

test('刷新后任务失效时同步清除续传预览和执行目标', () => {
  const {context} = harness();
  context.state.resumePreview = 'a';
  context.$('command-preview').value = 'python run.py demo.A';
  context.state.training.tasks = [];
  context.renderTrainingTasks();
  assert.equal(context.state.training.resumeSelected, null);
  assert.equal(context.state.resumePreview, null);
  assert.equal(context.$('resume-training').disabled, true);
  assert.equal(context.$('command-preview').value.includes('demo.A'), false);
});

test('修改草稿后点击继续训练仍将底部执行绑定到历史任务', async () => {
  const {context, pending} = harness();
  context.state.resumePreview = null;
  context.$('command-preview').value = 'python run.py demo.Form';
  const execution = context.executeCommand('a');
  assert.equal(pending[0].url, '/api/execute');
  assert.equal(JSON.parse(pending[0].options.body).train_resume, 'a');
  pending[0].resolve({run_id: 'run-a', display_command: 'python run.py demo.A'});
  await new Promise(setImmediate);
  pending[1].resolve({running: false, output: [], return_code: 0});
  await execution;
  assert.equal(context.$('command-preview').value, 'python run.py demo.A');
  assert.equal(context.state.resumePreview, 'a');
});

test('任务列表只展示训练状态，不展示分析正文', () => {
  const {context} = harness();
  context.state.training.tasks[0].training_status = 'Finished';
  context.renderTrainingTasks();
  const html = context.$('training-tasks').innerHTML;
  assert.ok(html.includes('Status'));
  assert.ok(html.includes('Finished'));
  assert.equal(html.includes('请忽略上文日志输出'), false);
});

test('选中任务的详情提供日志翻页，而不是直接展示全文', () => {
  assert.equal(page.includes('View log'), true);
  assert.equal(page.includes('data-log-where="end"'), true);
  assert.equal(page.includes('data-log-where="middle"'), true);
  assert.equal(page.includes('data-log-where="display"'), true);
  assert.equal(page.includes('loadTrainingTaskLog'), true);
});

test('切换任务后过期的日志响应不能显示到新任务上', async () => {
  const {context, pending} = harness();
  context.state.training.tasks.push({...context.state.training.tasks[0], task_id: 'b', strategy: 'demo.B'});
  const current = context.loadTrainingTaskLog('end');
  pending[0].resolve({name: 'a.log', page: 3, pages: 3, text: 'tail-line'});
  await current;
  assert.equal(context.$('training-task-log').hidden, false);
  assert.equal(context.$('training-task-log-text').value, 'tail-line');
  const stale = context.loadTrainingTaskLog('end');

  context.state.training.resumeSelected = 'b';
  const preview = context.previewTrainingTask('b');
  const generate = pending.find(item => item.url === '/api/generate');
  generate.resolve({display_command: 'resume-b', original_command: 'orig'});
  await preview;
  const logRequest = pending.filter(item => item.url === '/api/training/task-log').at(-1);
  logRequest.resolve({name: 'old.log', page: 1, pages: 1, text: 'secret-a'});
  await stale;
  assert.equal(context.$('training-task-log').hidden, true);
  assert.equal(String(context.$('training-task-log-text').value).includes('secret-a'), false);
});

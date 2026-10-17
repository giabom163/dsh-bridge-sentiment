"""Three bounded M3 scenarios in a fresh isolated directory. No network/model."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import storage
import analysis


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.limit != 3:
        parser.error('本定向检查固定 --limit 3')
    root = args.work_dir / ('m3-' + storage.now().replace(':', '').replace('+', '_'))
    root.mkdir(parents=True)
    db = storage.connect(root / 'fixture.db')
    analysis.init(db)
    with db:
        db.execute("INSERT INTO feeds VALUES(1,'隔离样本','https://example.invalid',?)", (storage.now(),))
    rows = [
        {'title':'引述反驳', 'summary':'有人声称女性不适合技术，我反对这种概括。', 'url':'https://example.invalid/1'},
        {'title':'来源冲突甲', 'summary':'来源甲称活动取消。', 'url':'https://example.invalid/2', 'published_at':'2026-10-14T00:00:00Z'},
        {'title':'来源冲突乙', 'summary':'来源乙称活动正常进行。', 'url':'https://example.invalid/3', 'published_at':'2026-10-14T00:00:00Z'}]
    run = storage.start_run(db, 1, 'isolated_m3', 3)
    storage.import_batch(db, root, run, rows, lambda _: '未复核')
    ids = [r[0] for r in db.execute('SELECT id FROM records ORDER BY title')]
    results = []
    def ref(r):
        return {'record_id':r['id'], 'raw_sha256':r['raw_sha256'], 'field':'summary', 'start':0, 'end':len(r['summary']), 'quote':r['summary']}
    def a_output(packet):
        return {'claims':[{'id':'c1','text':'作者赞同引述内容','target':'female','context':'assertion',
                           'citations':[ref(packet['records'][0])], 'uncertainty':'隔离反例，故意错误主张'}]}
    # 1. B rejects quotation-as-endorsement; hash/offset mismatch is rejected.
    first = analysis.create_job(db, root, 'quotation', [ids[0]], 3)
    def quote_adapter(role, packet, a):
        if role == 'A': return a_output(packet), None
        return {'reviews':[{'claim_id':'c1','verdict':'unsupported','reason':'原文明确反对该概括', 'counterevidence':[ref(packet['records'][0])]}]}, None
    analysis.request_once(db, first['id'], 'A', quote_adapter)
    final = analysis.request_once(db, first['id'], 'B', quote_adapter)
    assert final['status'] == 'blocked' and final['budget']['reserved'] == 2
    try:
        analysis.request_once(db, first['id'], 'B', quote_adapter)
        raise AssertionError('重复 B 未被阻止')
    except ValueError: pass
    bad = ref(first['packet']['records'][0]); bad['start'] = 1
    try:
        analysis.citation(first['packet'], bad)
        raise AssertionError('伪引用未被阻止')
    except ValueError: pass
    results.append({'case':'quotation_rebuttal_and_budget','status':'PASS','job_id':first['id']})
    # 2. Conflicting source statements remain unverifiable; unknown requests cannot retry.
    conflict_ids = [r[0] for r in db.execute("SELECT id FROM records WHERE title LIKE '来源冲突%' ORDER BY title")]
    second = analysis.create_job(db, root, 'conflict', conflict_ids, 3)
    def conflict_adapter(role, packet, a):
        if role == 'A': return a_output(packet), None
        return {'reviews':[{'claim_id':'c1','verdict':'unverifiable','reason':'两条来源说法冲突，不能确定事实', 'counterevidence':[ref(r) for r in packet['records']]}]}, None
    analysis.request_once(db, second['id'], 'A', conflict_adapter)
    assert analysis.request_once(db, second['id'], 'B', conflict_adapter)['status'] == 'blocked'
    uncertain = analysis.create_job(db, root, 'uncertain', [ids[0]], 3)
    with db:
        db.execute("INSERT INTO model_requests(job_id,role,status) VALUES(?,'A','reserved')", (uncertain['id'],))
        db.execute("UPDATE analysis_jobs SET status='running' WHERE id=?", (uncertain['id'],))
    db.close()
    db = storage.connect(root / 'fixture.db'); analysis.init(db); analysis.recover(db)
    assert analysis.job(db, uncertain['id'])['requests'][0]['status'] == 'uncertain'
    try:
        analysis.request_once(db, uncertain['id'], 'A', conflict_adapter)
        raise AssertionError('不确定请求被重发')
    except ValueError: pass
    results.append({'case':'source_conflict_and_restart','status':'PASS','job_id':second['id']})
    # 3. No complete-day baseline => insufficient, not 14 fabricated zero days.
    third = analysis.create_job(db, root, 'insufficient', conflict_ids, 3)
    assert third['id'] == analysis.create_job(db, root, 'insufficient', conflict_ids[::-1], 3)['id']
    assert third['budget']['reserved'] == 0
    assert analysis.candidate(db, third['id']) == analysis.candidate(db, third['id'])
    assert json.loads(analysis.candidate(db, third['id'])['metrics'])['reason'] == 'baseline_insufficient'
    assert analysis.heat_candidate([3]*14, 7, 0)['reason'] == 'mad_zero'
    assert analysis.heat_candidate([0]*14, 2, 0)['reason'] == 'low_sample'
    assert analysis.heat_candidate([], 1, 1)['level'] == 'quality'
    try:
        analysis.decide(db, third['id'], 'accept', '试图绕过复核')
        raise AssertionError('阻塞结论被接受')
    except ValueError: pass
    # Fully supported B still requires a separate manual decision.
    review = analysis.create_job(db, root, 'review', conflict_ids, 3)
    def supported(role, packet, a):
        if role == 'A': return a_output(packet), None
        return {'reviews':[{'claim_id':'c1','verdict':'supported','reason':'仅验证状态机的隔离响应，不代表事实', 'counterevidence':[]}]}, None
    analysis.request_once(db, review['id'], 'A', supported)
    assert analysis.request_once(db, review['id'], 'B', supported)['status'] == 'human_review'
    assert analysis.decide(db, review['id'], 'reject', '隔离状态机检查，不接受假设主张')['status'] == 'rejected'
    results.append({'case':'baseline_idempotency_and_human_gate','status':'PASS','job_id':third['id']})
    db.close()
    report = {'status':'PASS','scope':'isolated_three_scenarios_no_network_no_production_model','results':results,
              'fixture':str(root),'source_hashes':{name:storage.digest((Path(analysis.__file__).parent/name).read_bytes()) for name in ('analysis.py','storage.py')}}
    (root/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__ == '__main__':
    main()

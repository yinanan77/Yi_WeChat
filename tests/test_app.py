import unittest
from unittest.mock import patch
import sys
import os
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
import app

class TestOrchestrator(unittest.TestCase):
    def setUp(self):
        with app.lock:
            app.jobs.clear()
            app.call_times.clear()
            app.call_daily.clear()

    def test_planner_valid(self):
        self.assertEqual(app.parse_plan('```json\n{"tasks":["资料梳理","方案评估"]}\n```','fallback'),['资料梳理','方案评估'])
        self.assertEqual(app.parse_plan('{"tasks":["a","b","c","d"]}', 'fallback'),['fallback'])

    def test_free_model_guard(self):
        with patch.object(app,'API_KEY','fake'):
            with self.assertRaisesRegex(RuntimeError,'拒绝使用非免费模型'):
                app.llm([{'role':'user','content':'hey'}], 'openai/gpt-4.1')

    def test_budget(self):
        with patch.object(app,'DAILY_BUDGET',2):
            app.reserve_call();app.reserve_call()
            with self.assertRaisesRegex(RuntimeError,'今日免费模型调用上限'):
                app.reserve_call()

    def test_pipeline(self):
        def pretend(messages, model, job, label, api_key=None):
            if label=='拆解阶段':return '{"tasks":["A检查","B检查"]}'
            if label=='汇总阶段':return '最终汇总'
            return '已分析'+label
        job={'id':'a0a0a0a0a0','goal':'优化个人工作流程', 'status':'queued','created':app.now(),
             'updated':app.now(),'tasks':[],'events':[],'error':'','result':'','cancel_requested':False}
        with patch.object(app,'retry_llm',side_effect=pretend):
            app.run_job(job)
        self.assertEqual(job['status'],'done')
        self.assertEqual(job['result'],'最终汇总')
        self.assertEqual(len(job['tasks']),2)
        self.assertTrue(all(item['status']=='done' for item in job['tasks']))

    def test_failure_pipeline(self):
        job={'id':'a0a0a0a0a0','goal':'工作分析', 'status':'queued','created':app.now(),
             'updated':app.now(),'tasks':[],'events':[],'error':'','result':'','cancel_requested':False}
        with patch.object(app,'retry_llm',side_effect=RuntimeError('测试失败')):
            app.run_job(job)
        self.assertEqual(job['status'],'failed')
        self.assertEqual(job['error'],'测试失败')

if __name__ == '__main__':unittest.main()

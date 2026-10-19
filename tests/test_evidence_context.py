import unittest

from newspaper.evidence import context, selected_sources


class EvidenceContextTests(unittest.TestCase):
    def test_chinese_candidate_preserves_english_abstract_within_context_budget(self):
        prefix='Synthetic journal article header. '.ljust(2182,' ')
        abstract='Here we introduce a synthetic material. It was evaluated in a synthetic four-week experiment.'
        content=(prefix+abstract).ljust(6761,'x')
        source={'id':'s1','content':content}
        candidate={'source_ids':['s1'],'entities':['中文材料名'],'focus':'中文研究结果'}
        selected=selected_sources([source],[candidate])[0]['content']
        self.assertIn(abstract,selected)
        self.assertEqual(len(selected),6500)
        self.assertEqual(source['content'],content)

    def test_header_only_keyword_hits_do_not_waste_the_remaining_context_budget(self):
        text='SyntheticInstitute '.ljust(3000,' ')+'Detailed synthetic result.'+'x'*5000
        result=context(text,['SyntheticInstitute'])
        self.assertIn('Detailed synthetic result.',result)
        self.assertLessEqual(len(result),6500)

    def test_deep_evidence_still_survives_when_keywords_match_beyond_the_lead(self):
        text='lead '*1000+'x'*7000+'TARGET-ENTITY measured synthetic result.'+'y'*7000
        result=context(text,['TARGET-ENTITY'])
        self.assertIn('TARGET-ENTITY measured synthetic result.',result)
        self.assertLessEqual(len(result),6500)

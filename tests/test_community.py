import unittest
from unittest.mock import MagicMock
from fs25_network_core.community import CommunityApplications, requested_server_nickname

class CommunityTests(unittest.TestCase):
    def setUp(self): self.db = MagicMock(); self.app = CommunityApplications(MagicMock(db=self.db))
    def test_validation(self):
        self.assertEqual(requested_server_nickname(' Repton ', ' Farm '), ('Repton','Farm','Repton | Farm'))
        for values in [('', 'Farm'), ('Name',''), ('A|B','Farm'), ('A','x'*30)]:
            with self.assertRaises(ValueError): requested_server_nickname(*values)
    def test_identical_pending_is_idempotent_and_conflict_rejected(self):
        self.db.community_applications.find_one.return_value = {'state':'pending','nickname':'R','farm_name':'F'}
        self.assertEqual(self.app.apply('1','R','F')['state'], 'pending')
        with self.assertRaises(ValueError): self.app.apply('1','Other','F')

    def test_approval_is_idempotent_after_authoritative_state_is_committed(self):
        result = MagicMock(modified_count=0)
        self.db.community_applications.update_one.return_value = result
        self.db.community_applications.find_one.side_effect = [
            {'_id': '1', 'state': 'approved', 'server_nickname': 'R | F'},
            {'_id': '1', 'state': 'approved', 'server_nickname': 'R | F'}]
        record = self.app.approve('1', 'staff')
        self.assertEqual(record['state'], 'approved')

import unittest

from src.sql_firewall import extract_blocked_ip, get_firewall_retry_delay, is_sql_firewall_block_error


class SqlFirewallHelpersTest(unittest.TestCase):
    def test_extract_blocked_ip_from_sql_error(self):
        error_message = (
            "[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
            "Cannot open server 'infraforge-sql' requested by the login. "
            "Client with IP address '12.116.161.158' is not allowed to access the server."
        )

        self.assertEqual(extract_blocked_ip(error_message), "12.116.161.158")

    def test_extract_blocked_ip_returns_none_for_unrelated_error(self):
        self.assertIsNone(extract_blocked_ip("Login failed for user"))

    def test_firewall_error_detection_matches_sql_message(self):
        self.assertTrue(
            is_sql_firewall_block_error(
                "Client with IP address '12.116.161.158' is not allowed to access the server"
            )
        )
        self.assertFalse(is_sql_firewall_block_error("Login timeout expired"))

    def test_retry_delay_scales_with_attempts(self):
        self.assertGreaterEqual(get_firewall_retry_delay(1), get_firewall_retry_delay(0))
        self.assertGreaterEqual(get_firewall_retry_delay(2), get_firewall_retry_delay(1))


if __name__ == "__main__":
    unittest.main()
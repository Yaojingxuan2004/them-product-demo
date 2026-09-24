"""No real API calls: locate failure phases and avoid leaking raw response text."""
import json
import unittest
from unittest.mock import patch
from providers import DeepSeekProvider, ProviderFailure


class Response:
    def __init__(self,body):self.body=body
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,size):return self.body


class DiagnosticsChecks(unittest.TestCase):
    def call_error(self, body):
        with patch('providers.urlopen',return_value=Response(body)):
            with self.assertRaises(ProviderFailure) as caught:
                DeepSeekProvider('synthetic-token','fixture').invoke({'messages':[]})
        return caught.exception

    def test_non_ascii_key_stops_before_network(self):
        with patch('providers.urlopen') as network:
            with self.assertRaises(ValueError) as caught:DeepSeekProvider('synthetic-中文','fixture')
            network.assert_not_called()
        self.assertNotIn('synthetic-',str(caught.exception))

    def test_local_serialization_error_is_not_a_response_error(self):
        with patch('providers.urlopen') as network:
            with self.assertRaises(ProviderFailure) as caught:
                DeepSeekProvider('synthetic-token','fixture').invoke({'bad':object()})
        network.assert_not_called();self.assertEqual(caught.exception.state,'failed')
        self.assertEqual(caught.exception.diagnostics['phase'],'request')

    def test_transport_value_error_is_not_mislabeled_decode(self):
        with patch('providers.urlopen',side_effect=ValueError('DO_NOT_LOG')):
            with self.assertRaises(ProviderFailure) as caught:
                DeepSeekProvider('synthetic-token','fixture').invoke({'messages':[]})
        self.assertEqual(caught.exception.diagnostics['phase'],'transport')
        self.assertNotIn('DO_NOT_LOG',str(caught.exception.message))

    def test_bad_outer_json_reports_length_without_body(self):
        error=self.call_error(b'<html>SECRET_FIXTURE</html>')
        self.assertEqual(error.diagnostics['phase'],'decode')
        self.assertEqual(error.diagnostics['response_bytes'],27)
        self.assertNotIn('SECRET_FIXTURE',json.dumps(error.diagnostics))

    def test_bad_schema_reports_shape_not_error_payload(self):
        for body in [b'[]',b'{"choices":[]}',b'{"error":"SECRET_FIXTURE"}',
                     b'{"choices":[{"message":{"content":null}}]}']:
            with self.subTest(body=body):
                error=self.call_error(body)
                self.assertEqual(error.diagnostics['phase'],'schema')
                self.assertNotIn('SECRET_FIXTURE',json.dumps(error.diagnostics))


if __name__=='__main__':unittest.main()

import unittest

from src.pipeline_helpers import validate_arm_expression_syntax
from src.template_engine import build_composite_validation_template


class ArmTemplateValidationTest(unittest.TestCase):
    def test_composite_validation_uses_unwrapped_arm_expression_in_resource_id(self):
        parent = {
            "parameters": {"vnetName": {"type": "string"}},
            "resources": [
                {
                    "type": "Microsoft.Network/virtualNetworks",
                    "apiVersion": "2024-01-01",
                    "name": "[parameters('vnetName')]",
                }
            ],
        }
        child = {
            "resources": [
                {
                    "type": "Microsoft.Network/virtualNetworks/subnets",
                    "apiVersion": "2024-01-01",
                    "name": "[concat(parameters('vnetName'), '/default')]",
                }
            ]
        }

        composite = build_composite_validation_template(parent, child)
        depends_on = composite["resources"][1]["dependsOn"]

        self.assertIn(
            "[resourceId('Microsoft.Network/virtualNetworks', parameters('vnetName'))]",
            depends_on,
        )
        self.assertNotIn(
            "[resourceId('Microsoft.Network/virtualNetworks', [parameters('vnetName')])]",
            depends_on,
        )

    def test_validate_arm_expression_syntax_flags_nested_bracketed_function_args(self):
        template = {
            "resources": [
                {
                    "type": "Microsoft.Network/virtualNetworks/subnets",
                    "name": "[concat(parameters('vnetName'), '/default')]",
                    "dependsOn": [
                        "[resourceId('Microsoft.Network/virtualNetworks', [parameters('vnetName')])]"
                    ],
                }
            ]
        }

        errors = validate_arm_expression_syntax(template)

        self.assertEqual(len(errors), 1)
        self.assertIn("resourceId() argument", errors[0])

    def test_validate_arm_expression_syntax_allows_unwrapped_function_args(self):
        template = {
            "resources": [
                {
                    "type": "Microsoft.Network/virtualNetworks/subnets",
                    "name": "[concat(parameters('vnetName'), '/default')]",
                    "dependsOn": [
                        "[resourceId('Microsoft.Network/virtualNetworks', parameters('vnetName'))]"
                    ],
                }
            ]
        }

        self.assertEqual(validate_arm_expression_syntax(template), [])

    def test_validate_arm_expression_syntax_rejects_utcnow_outside_parameter_defaults(self):
        template = {
            "variables": {
                "deploymentDate": "[utcNow('yyyy-MM-dd')]"
            },
            "resources": [
                {
                    "type": "Microsoft.Network/applicationGateways",
                    "name": "infraforge-appgw-dev-eus2-001",
                    "tags": {
                        "deployedAt": "[utcNow('yyyy-MM-dd')]"
                    },
                }
            ],
        }

        errors = validate_arm_expression_syntax(template)

        self.assertEqual(len(errors), 2)
        self.assertTrue(all("utcNow() is only allowed in parameter defaultValue expressions" in error for error in errors))

    def test_validate_arm_expression_syntax_allows_utcnow_in_parameter_default(self):
        template = {
            "parameters": {
                "deploymentDate": {
                    "type": "string",
                    "defaultValue": "[utcNow('yyyy-MM-dd')]",
                }
            }
        }

        self.assertEqual(validate_arm_expression_syntax(template), [])


if __name__ == "__main__":
    unittest.main()
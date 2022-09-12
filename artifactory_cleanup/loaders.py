import importlib
import inspect
import logging
import os.path
import sys
from copy import deepcopy
from pathlib import Path
from typing import List, Tuple, Type, Dict, Union

import cfgv
import yaml

from artifactory_cleanup import rules
from artifactory_cleanup.errors import InvalidConfigError
from artifactory_cleanup.rules import Repo
from artifactory_cleanup.rules.base import CleanupPolicy, Rule

logger = logging.getLogger("artifactory-cleanup")

RULE_SCHEMA = cfgv.Map(
    "Rule",
    "rule",
    cfgv.Required("rule", cfgv.check_string),
)


def _get_check_fn(annotation):
    if annotation is int:
        return cfgv.check_int
    if annotation is str:
        return cfgv.check_string
    return cfgv.check_any


class SchemaBuilder:
    def _get_rule_conditionals(self, name, rule) -> List[cfgv.Conditional]:
        if rule.schema is not None:
            return rule.schema
        conditionals = []
        params = list(inspect.signature(rule.__init__).parameters.values())
        ignore = {"self", "args", "kwargs"}
        for param in params:
            if param.name in ignore:
                continue
            if param.annotation is param.empty:
                check_fn = cfgv.check_any
            else:
                check_fn = _get_check_fn(param.annotation)

            if param.default is not param.empty:
                cond = cfgv.ConditionalOptional(
                    param.name,
                    check_fn,
                    param.default,
                    "rule",
                    cfgv.In(name),
                    ensure_absent=False,
                )
            else:
                cond = cfgv.Conditional(
                    param.name,
                    check_fn,
                    "rule",
                    cfgv.In(name),
                    ensure_absent=False,
                )
            conditionals.append(cond)
        return conditionals

    def get_rules_conditionals(self, rules) -> List[cfgv.Conditional]:
        conditionals = []
        for name, rule in rules.items():
            conditionals.extend(self._get_rule_conditionals(name, rule))
        return conditionals

    def get_root_schema(self, rules):
        conditionals = self.get_rules_conditionals(rules)
        rules_names = list(rules.keys())
        rule_schema = cfgv.Map(
            "Rule",
            "rule",
            cfgv.Required("rule", cfgv.check_string),
            cfgv.Required("rule", cfgv.check_one_of(rules_names)),
            *conditionals,
        )
        policy_schema = cfgv.Map(
            "Policy",
            "name",
            cfgv.Required("name", cfgv.check_string),
            cfgv.RequiredRecurse("rules", cfgv.Array(rule_schema)),
        )

        config_schema = cfgv.Map(
            "Config",
            None,
            cfgv.Required("server", cfgv.check_string),
            cfgv.Required("user", cfgv.check_string),
            cfgv.Required("password", cfgv.check_string),
            cfgv.RequiredRecurse("policies", cfgv.Array(policy_schema)),
        )

        root_schema = cfgv.Map(
            "Artifactory Cleanup",
            None,
            cfgv.RequiredRecurse("artifactory-cleanup", config_schema),
        )
        return root_schema


class RuleRegistry:
    def __init__(self):
        self.rules: Dict[str, Type[Rule]] = {}

    def get(self, name: str) -> Type[Rule]:
        return self.rules[name]

    def register(self, rule: Type[Rule], name=None, warning=True):
        name = name or rule.name()
        if name in self.rules and warning:
            logger.warning(f"Rule with a name '{name}' has been registered before.")
            return
        self.rules[name] = rule

    def register_builtin_rules(self):
        for name, obj in vars(rules).items():
            if inspect.isclass(obj) and issubclass(obj, Rule):
                self.register(obj, warning=False)


registry = RuleRegistry()
registry.register_builtin_rules()


class YamlConfigLoader:
    """
    Load configuration and policies from yaml file
    """

    _rules = {}

    def __init__(self, filepath):
        self.filepath = Path(filepath)

    def get_policies(self) -> List[CleanupPolicy]:
        config = self.load(self.filepath)
        policies = []

        for policy_data in config["artifactory-cleanup"]["policies"]:
            policy_name = policy_data["name"]
            rules = []
            for rule_data in policy_data["rules"]:
                try:
                    rule = self._build_rule(rule_data)
                except Exception as exc:
                    print(
                        f"Failed to initialize '{rule_data['rule']}' in {policy_name}:",
                        file=sys.stdout,
                    )
                    print(exc, file=sys.stdout)
                    sys.exit(1)

                rules.append(rule)
            policy = CleanupPolicy(policy_name, *rules)
            policies.append(policy)
        return policies

    def _build_rule(self, rule_data: Dict) -> Union[Rule, Type[Rule]]:
        kwargs = deepcopy(rule_data)
        rule_cls = registry.get(kwargs.pop("rule"))

        # For Repo rule, CleanupPolicy initialize it later with the name of the policy
        if rule_cls == Repo and not kwargs:
            return rule_cls

        return rule_cls(**kwargs)

    @staticmethod
    def load(filename):
        schema = SchemaBuilder().get_root_schema(registry.rules)
        return cfgv.load_from_filename(
            filename, schema, yaml.safe_load, InvalidConfigError
        )

    def get_connection(self) -> Tuple[str, str, str]:
        config = self.load(self.filepath)
        server = config["artifactory-cleanup"]["server"]
        user = config["artifactory-cleanup"]["user"]
        password = config["artifactory-cleanup"]["password"]

        user = os.path.expandvars(user)
        password = os.path.expandvars(password)
        return server, user, password


class PythonLoader:
    """
    Load policies and rules from python file + connections settings from cli
    """

    def __init__(self, filepath, cli):
        self.filepath = Path(filepath)
        self.cli = cli

    @staticmethod
    def import_module(filename):
        filepath = Path(filename)
        directory = filepath.parent
        sys.path.append(str(directory))
        # Get module name without the py suffix: policies.py => policies
        module_name = filepath.stem
        return importlib.import_module(module_name)

    def get_policies(self) -> List[CleanupPolicy]:
        try:
            policies_module = self.import_module(self.filepath)
            policies = getattr(policies_module, "RULES")

            # Validate that all policies is CleanupPolicy
            for policy in policies:
                if not isinstance(policy, CleanupPolicy):
                    sys.exit(f"Policy '{policy}' is not CleanupPolicy, check it please")

            return policies
        except ImportError as error:
            print("Error: {}".format(error))
            sys.exit(1)

    def get_connection(self) -> Tuple[str, str, str]:
        server = self.cli._artifactory_server
        user = self.cli._user
        password = self.cli._password
        if not server:
            print("--artifactory-server is required for python config", file=sys.stdout)
            self.cli.help()
            exit(1)
        if not user:
            print("--user is required for python config", file=sys.stdout)
            self.cli.help()
            exit(1)
        if not password:
            print("--password is required for python config", file=sys.stdout)
            self.cli.help()
            exit(1)

        return server, user, password

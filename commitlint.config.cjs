module.exports = {
  extends: ['@commitlint/config-conventional'],
  rules: {
    'type-enum': [2, 'always', [
      'feat', 'fix', 'refactor', 'style', 'a11y', 'docs', 'test', 'chore', 'perf', 'ci'
    ]],
    'scope-enum': [2, 'always', [
      'admin-web', 'app', 'api',
      'backend', 'api-gateway', 'user-service', 'content-service', 'ai-service',
      'android', 'theme', 'ui',
      'deps', 'config', 'infra'
    ]],
    'subject-max-length': [2, 'always', 72],
    'subject-case': [2, 'always', 'lower-case'],
  }
};

// Alerts the InfoSec LLD §8 asks for in the "Now" phase: failures are seen at once, not reported by users.
// One action group mails alertEmails; with no address the alerts still fire and show in the portal.
param namePrefix string
param tags object = {}
param containerAppId string
param postgresServerId string
@description('Addresses that receive the alerts. Empty: no action group; alerts show in Azure Monitor only.')
param alertEmails array = []

var actionGroupName = '${namePrefix}-alerts'
var actions = empty(alertEmails) ? [] : [
  {
    actionGroupId: resourceId('Microsoft.Insights/actionGroups', actionGroupName)
  }
]

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (!empty(alertEmails)) {
  name: actionGroupName
  location: 'global'
  tags: tags
  properties: {
    groupShortName: take('invstudio', 12)
    enabled: true
    emailReceivers: [for (address, i) in alertEmails: {
      name: 'email-${i}'
      emailAddress: address
      useCommonAlertSchema: true
    }]
  }
}

var rules = [
  {
    name: '${namePrefix}-app-restarts'
    text: 'The app container restarted. A crash or out-of-memory; check ContainerAppSystemLogs_CL.'
    severity: 1
    scope: containerAppId
    namespace: 'Microsoft.App/containerApps'
    metric: 'RestartCount'
    aggregation: 'Total'
    threshold: 0
    window: 'PT15M'
    dimensions: []
  }
  {
    name: '${namePrefix}-app-5xx'
    text: 'More than 5 server errors in 5 minutes. Each 500 carries an X-Request-Id that is in the app log.'
    severity: 2
    scope: containerAppId
    namespace: 'Microsoft.App/containerApps'
    metric: 'Requests'
    aggregation: 'Total'
    threshold: 5
    window: 'PT5M'
    dimensions: [
      {
        name: 'statusCodeCategory'
        operator: 'Include'
        values: [
          '5xx'
        ]
      }
    ]
  }
  {
    name: '${namePrefix}-db-cpu'
    text: 'Database CPU above 80% for 15 minutes. Burstable B2s runs out of credits under sustained load.'
    severity: 2
    scope: postgresServerId
    namespace: 'Microsoft.DBforPostgreSQL/flexibleServers'
    metric: 'cpu_percent'
    aggregation: 'Average'
    threshold: 80
    window: 'PT15M'
    dimensions: []
  }
  {
    name: '${namePrefix}-db-storage'
    text: 'Database storage above 85%. Grow postgresStorageGB before it fills.'
    severity: 2
    scope: postgresServerId
    namespace: 'Microsoft.DBforPostgreSQL/flexibleServers'
    metric: 'storage_percent'
    aggregation: 'Average'
    threshold: 85
    window: 'PT30M'
    dimensions: []
  }
]

resource alerts 'Microsoft.Insights/metricAlerts@2018-03-01' = [for rule in rules: {
  name: rule.name
  location: 'global'
  tags: tags
  dependsOn: [
    actionGroup
  ]
  properties: {
    description: rule.text
    severity: rule.severity
    enabled: true
    scopes: [
      rule.scope
    ]
    evaluationFrequency: 'PT5M'
    windowSize: rule.window
    targetResourceType: rule.namespace
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          criterionType: 'StaticThresholdCriterion'
          name: 'threshold'
          metricNamespace: rule.namespace
          metricName: rule.metric
          timeAggregation: rule.aggregation
          operator: 'GreaterThan'
          threshold: rule.threshold
          dimensions: rule.dimensions
        }
      ]
    }
    actions: actions
  }
}]

output alertNames array = [for rule in rules: rule.name]

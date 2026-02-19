"""
CI/CD pipeline reference patterns for GitHub Actions and Azure DevOps.
"""


def get_github_actions_reference(app_type: str = "python") -> str:
    """Return GitHub Actions best-practice reference patterns."""
    build_steps = _get_build_steps_gha(app_type)

    return f"""
#### Workflow Structure Pattern
```yaml
name: CI/CD Pipeline

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:
    inputs:
      environment:
        description: 'Target environment'
        required: true
        default: 'dev'
        type: choice
        options: [dev, staging, prod]

permissions:
  id-token: write   # For OIDC
  contents: read
  security-events: write  # For CodeQL

concurrency:
  group: ${{{{ github.workflow }}}}-${{{{ github.ref }}}}
  cancel-in-progress: true
```

#### Build Job Pattern
```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

{build_steps}

      - name: Upload artifact
        uses: actions/upload-artifact@v4
        with:
          name: app-artifact
          path: ./build-output
```

#### Security Scanning Pattern
```yaml
  security-scan:
    runs-on: ubuntu-latest
    needs: build
    steps:
      - uses: actions/checkout@v4

      - name: Initialize CodeQL
        uses: github/codeql-action/init@v3
        with:
          languages: ${{{{ matrix.language }}}}

      - name: Perform CodeQL Analysis
        uses: github/codeql-action/analyze@v3

      - name: Dependency Review
        if: github.event_name == 'pull_request'
        uses: actions/dependency-review-action@v4
```

#### Deploy Job Pattern (with OIDC)
```yaml
  deploy-dev:
    runs-on: ubuntu-latest
    needs: [build, security-scan]
    environment: dev
    steps:
      - name: Download artifact
        uses: actions/download-artifact@v4
        with:
          name: app-artifact

      - name: Azure Login (OIDC)
        uses: azure/login@v2
        with:
          client-id: ${{{{ secrets.AZURE_CLIENT_ID }}}}
          tenant-id: ${{{{ secrets.AZURE_TENANT_ID }}}}
          subscription-id: ${{{{ secrets.AZURE_SUBSCRIPTION_ID }}}}

      - name: Deploy to Azure App Service
        uses: azure/webapps-deploy@v3
        with:
          app-name: ${{{{ vars.APP_SERVICE_NAME }}}}
          package: .

  deploy-prod:
    runs-on: ubuntu-latest
    needs: deploy-staging
    environment:
      name: prod
      url: ${{{{ steps.deploy.outputs.webapp-url }}}}
    steps:
      # Same as above — production requires manual approval via environment protection rules
```

#### Reusable Workflow Pattern
```yaml
# .github/workflows/deploy-template.yml
on:
  workflow_call:
    inputs:
      environment:
        required: true
        type: string
      app-name:
        required: true
        type: string
    secrets:
      AZURE_CLIENT_ID:
        required: true
      AZURE_TENANT_ID:
        required: true
      AZURE_SUBSCRIPTION_ID:
        required: true
```
"""


def get_azure_devops_reference(app_type: str = "dotnet") -> str:
    """Return Azure DevOps best-practice reference patterns."""
    build_steps = _get_build_steps_ado(app_type)

    return f"""
#### Pipeline Structure Pattern
```yaml
trigger:
  branches:
    include: [main]
  paths:
    exclude: ['docs/*', '*.md']

pr:
  branches:
    include: [main]

pool:
  vmImage: 'ubuntu-latest'

variables:
  - group: 'infraforge-common'
  - name: buildConfiguration
    value: 'Release'
```

#### Stages Pattern
```yaml
stages:
  - stage: Build
    displayName: 'Build & Test'
    jobs:
      - job: BuildJob
        steps:
{build_steps}

          - task: PublishBuildArtifacts@1
            inputs:
              pathToPublish: '$(Build.ArtifactStagingDirectory)'
              artifactName: 'drop'

  - stage: DeployDev
    displayName: 'Deploy to Dev'
    dependsOn: Build
    condition: and(succeeded(), eq(variables['Build.SourceBranch'], 'refs/heads/main'))
    jobs:
      - deployment: DeployDev
        environment: 'dev'
        strategy:
          runOnce:
            deploy:
              steps:
                - task: AzureWebApp@1
                  inputs:
                    azureSubscription: 'azure-service-connection'
                    appName: '$(appName)-dev'
                    package: '$(Pipeline.Workspace)/drop/**/*.zip'

  - stage: DeployProd
    displayName: 'Deploy to Production'
    dependsOn: DeployStaging
    condition: succeeded()
    jobs:
      - deployment: DeployProd
        environment: 'prod'  # Has approval gates configured
        strategy:
          runOnce:
            deploy:
              steps:
                - task: AzureWebApp@1
                  inputs:
                    azureSubscription: 'azure-service-connection-prod'
                    appName: '$(appName)-prod'
                    package: '$(Pipeline.Workspace)/drop/**/*.zip'
```

#### Template Usage Pattern
```yaml
# templates/deploy.yml
parameters:
  - name: environment
    type: string
  - name: serviceConnection
    type: string
  - name: appName
    type: string

stages:
  - stage: Deploy_${{{{ parameters.environment }}}}
    displayName: 'Deploy to ${{{{ parameters.environment }}}}'
    jobs:
      - deployment: Deploy
        environment: '${{{{ parameters.environment }}}}'
        strategy:
          runOnce:
            deploy:
              steps:
                - task: AzureWebApp@1
                  inputs:
                    azureSubscription: '${{{{ parameters.serviceConnection }}}}'
                    appName: '${{{{ parameters.appName }}}}'
                    package: '$(Pipeline.Workspace)/drop/**/*.zip'

# Main pipeline references template:
stages:
  - template: templates/deploy.yml
    parameters:
      environment: 'dev'
      serviceConnection: 'azure-dev'
      appName: 'myapp-dev'
```
"""


def _get_build_steps_gha(app_type: str) -> str:
    """Return language-specific build steps for GitHub Actions."""
    steps = {
        "python": """      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run tests
        run: pytest --junitxml=test-results.xml --cov=src

      - name: Upload test results
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: test-results
          path: test-results.xml""",

        "node": """      - name: Set up Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'

      - name: Install dependencies
        run: npm ci

      - name: Run tests
        run: npm test

      - name: Build
        run: npm run build""",

        "dotnet": """      - name: Set up .NET
        uses: actions/setup-dotnet@v4
        with:
          dotnet-version: '8.0.x'

      - name: Restore dependencies
        run: dotnet restore

      - name: Build
        run: dotnet build --configuration Release --no-restore

      - name: Test
        run: dotnet test --no-build --configuration Release --logger trx

      - name: Publish
        run: dotnet publish -c Release -o ./publish""",

        "java": """      - name: Set up JDK
        uses: actions/setup-java@v4
        with:
          java-version: '17'
          distribution: 'temurin'
          cache: 'maven'

      - name: Build with Maven
        run: mvn -B package --file pom.xml

      - name: Run tests
        run: mvn test""",

        "container": """      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Login to ACR
        uses: azure/docker-login@v2
        with:
          login-server: ${{ secrets.ACR_LOGIN_SERVER }}
          username: ${{ secrets.ACR_USERNAME }}
          password: ${{ secrets.ACR_PASSWORD }}

      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          push: true
          tags: ${{ secrets.ACR_LOGIN_SERVER }}/myapp:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max""",
    }
    return steps.get(app_type, steps["python"])


def _get_build_steps_ado(app_type: str) -> str:
    """Return language-specific build steps for Azure DevOps."""
    steps = {
        "python": """          - task: UsePythonVersion@0
            inputs:
              versionSpec: '3.12'

          - script: pip install -r requirements.txt
            displayName: 'Install dependencies'

          - script: pytest --junitxml=$(Build.ArtifactStagingDirectory)/test-results.xml
            displayName: 'Run tests'

          - task: PublishTestResults@2
            inputs:
              testResultsFormat: 'JUnit'
              testResultsFiles: '$(Build.ArtifactStagingDirectory)/test-results.xml'""",

        "dotnet": """          - task: DotNetCoreCLI@2
            inputs:
              command: 'restore'
            displayName: 'Restore packages'

          - task: DotNetCoreCLI@2
            inputs:
              command: 'build'
              arguments: '--configuration $(buildConfiguration)'
            displayName: 'Build'

          - task: DotNetCoreCLI@2
            inputs:
              command: 'test'
              arguments: '--configuration $(buildConfiguration) --logger trx'
            displayName: 'Run tests'

          - task: DotNetCoreCLI@2
            inputs:
              command: 'publish'
              arguments: '-c $(buildConfiguration) -o $(Build.ArtifactStagingDirectory)'
            displayName: 'Publish'""",

        "node": """          - task: NodeTool@0
            inputs:
              versionSpec: '20.x'
            displayName: 'Setup Node.js'

          - script: npm ci
            displayName: 'Install dependencies'

          - script: npm test
            displayName: 'Run tests'

          - script: npm run build
            displayName: 'Build'""",

        "java": """          - task: Maven@3
            inputs:
              mavenPomFile: 'pom.xml'
              goals: 'package'
              publishJUnitResults: true
              testResultsFiles: '**/surefire-reports/TEST-*.xml'
            displayName: 'Maven Build & Test'""",

        "container": """          - task: Docker@2
            inputs:
              containerRegistry: 'acr-service-connection'
              repository: 'myapp'
              command: 'buildAndPush'
              Dockerfile: '**/Dockerfile'
              tags: '$(Build.BuildId)'
            displayName: 'Build & Push Container'""",
    }
    return steps.get(app_type, steps["dotnet"])

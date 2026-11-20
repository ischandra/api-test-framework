// API test pipeline. Needs python3 (3.9+) on the agent.
// For real environments, add credentials in Jenkins and map them to the
// variables your environments/<env>.yaml reads (see the commented block).
pipeline {
    agent any

    parameters {
        choice(name: 'API_ENV', choices: ['local', 'dev'], description: 'environments/<name>.yaml ("local" starts the bundled demo API)')
        string(name: 'TEST_SELECTION', defaultValue: '', description: 'Optional pytest filter, e.g. -m smoke  or  -k Users')
        string(name: 'SEED', defaultValue: '', description: 'Data seed to reproduce an earlier run (default: build number)')
    }

    options {
        timestamps()
        timeout(time: 30, unit: 'MINUTES')
    }

    // environment {
    //     API_USERNAME = credentials('api-username')
    //     API_PASSWORD = credentials('api-password')
    // }

    stages {
        stage('Setup') {
            steps {
                sh '''
                    python3 -m venv .venv
                    . .venv/bin/activate
                    pip install -q --upgrade pip
                    pip install -q -r requirements.txt
                '''
            }
        }

        stage('Framework self-tests') {
            steps {
                sh '. .venv/bin/activate && pytest selftests -q --junitxml=reports/selftests.xml'
            }
        }

        stage('Start demo API') {
            when { expression { params.API_ENV == 'local' } }
            steps {
                sh '''
                    . .venv/bin/activate
                    mkdir -p reports
                    nohup python -m apitest demo-server --port 8000 > reports/demo-server.log 2>&1 &
                    echo $! > .demo-server.pid
                    for i in $(seq 1 20); do curl -fs http://localhost:8000/health >/dev/null && exit 0; sleep 0.5; done
                    echo "demo API did not start" && exit 1
                '''
            }
        }

        stage('API tests') {
            steps {
                // parameters are read as shell variables (not Groovy-interpolated)
                sh '''
                    . .venv/bin/activate
                    pytest --env "$API_ENV" --seed "${SEED:-$BUILD_NUMBER}" \
                           --junitxml=reports/api-tests.xml $TEST_SELECTION
                '''
            }
        }
    }

    post {
        always {
            sh 'if [ -f .demo-server.pid ]; then kill $(cat .demo-server.pid) || true; rm -f .demo-server.pid; fi'
            junit allowEmptyResults: true, testResults: 'reports/*.xml'
            archiveArtifacts artifacts: 'reports/**', allowEmptyArchive: true
        }
    }
}

# nvis-rack-status

    ## 🎉 Repository Bootstrapped by Fusion

    This repository has been configured with an automated CI/CD pipeline.

    ### ✅ What Was Configured

    1. **GitLab CI/CD Pipeline** (`.gitlab-ci.yml`)
       - Automated Docker image building
       - Image tagging with semantic versioning
       - Push to JFrog Artifactory: `artifactory.nvidia.com/it-astra-docker-local`

    2. **GitLab Runners**
       - Shared/instance runners enabled for CI/CD execution
       - Runner tag: `fusion-instance-k8s`

    3. **CI Automation Token**
       - Project access token created: `CI_TOOLKIT_VERSION_BOT`
       - Automatically manages version updates

    ### 🚀 Next Steps

    1. **Add Your Application Code**
       - Add your Dockerfile(s) to your project
       - Add your source code and dependencies
       - Push changes to trigger the CI/CD pipeline

    2. **Monitor Your Builds**
       - View pipeline status: [nvis-rack-status Pipelines](https://gitlab-master.nvidia.com/nvis/nvis-rack-status/-/pipelines)
       - Check build logs for any issues

    3. **Access Built Images**
       - Images will be available at: `artifactory.nvidia.com/it-astra-docker-local/{app-name}:{version}`

    ### Maintainers
    Giovanni Mascari - gmascari@nvidia.com
    
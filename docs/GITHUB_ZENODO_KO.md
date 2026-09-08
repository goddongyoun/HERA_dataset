# GitHub 업로드와 Zenodo DOI 안내

이 문서는 [goddongyoun/HERA_dataset](https://github.com/goddongyoun/HERA_dataset)의 HERA 코드·결과와 별도 원시 데이터 공개 절차 안내입니다. clone과 로컬 commit, 원격 push, 공개 태그·Release 생성, Zenodo 게시 및 DOI 등록은 각각 다른 단계입니다. GitHub에 코드가 반영되었다고 해서 DOI가 발급되거나 원시 데이터까지 공개된 것은 아닙니다.

## 현재 완료 상태

2026년 9월 8일 기준으로 GitHub 저장소는 공개되어 있으며, 전체 `main_001` 원시 증거는 **HERA 데이터셋**으로 Zenodo에 게시됐습니다. 특정 데이터 버전 DOI는 [10.5281/zenodo.22652578](https://doi.org/10.5281/zenodo.22652578)입니다. 이 DOI는 논문 DOI나 별도로 발급된 소프트웨어 DOI가 아닙니다.

- [HERA_dataset.zip 다운로드](https://zenodo.org/api/records/22652578/files/HERA_dataset.zip/content)
- [SHA-256 파일 다운로드](https://zenodo.org/api/records/22652578/files/HERA_dataset.sha256/content)
- ZIP 크기: 243,938,238바이트
- SHA-256: `59c9fff6af4e71de9dc4b500d497e3d326df14abdf95b2ef07f93cc7866ec1a5`
- 데이터에 포함된 코드 커밋: `82ae54b209389ef4917b857bdd14f9baf14ca322`

비로그인 전체 ZIP 다운로드와 체크섬 검증을 완료했습니다. 새 폴더 재분석에서 회귀 테스트 76개, 저장 분석 11개, 결과 표 4개도 검증했습니다. 자세한 검증 시각과 범위는 [DATA_RELEASE.json](DATA_RELEASE.json)에 있습니다. 이번 안내문 갱신으로 원시 데이터나 게시된 ZIP을 바꾸지는 않았습니다. ZIP 안의 설명서는 포장 당시의 상태를 보존하며, 게시 후 상태는 현재 저장소의 안내를 따릅니다.

아래의 업로드·DOI 절차는 이후 버전이나 선택적인 별도 소프트웨어 보관을 위한 안내입니다. 지금 데이터 DOI를 다시 발급받거나 소프트웨어 DOI를 추가로 받아야 현재 데이터를 인용할 수 있는 것은 아닙니다.

## 1. 현재 공개 범위

현재 저장소 트리는 신규 **1,472회 `main_001` 실험**의 코드·매니페스트·요약 결과·그림으로 구성됩니다. 구버전 360회 자료인 `full/`, `summary/`와 별도 설명서는 현재 트리에서 삭제했습니다. 과거 커밋에는 남아 있어 복구할 수 있으며 이력 재작성은 하지 않았습니다. 두 실험의 수치는 합치지 않습니다.

| 포함 | 제외 |
|---|---|
| 신규 실행·분석 코드와 테스트 | 원시 trial/event/control trace 전체 |
| 고정 실험 매니페스트와 프로토콜 | 원고·MDPI 양식·리뷰어 답변 |
| 신규 요약 결과와 최종 그림 | 서버 로그·모델 가중치·이전 실험 자료 |

GitHub 폴더만으로 요약 결과를 확인하고 제공된 코드를 사용할 수 있지만, **전체 원시 기록에서 재계산하려면 별도 데이터 ZIP도 필요**합니다. 위에 게시된 Zenodo ZIP에는 해당 원시 증거와 맞는 코드 스냅샷이 포함되어 있습니다.

기존 전체 evidence ZIP은 리뷰 답변과 로컬 경로 등 공개용으로 정리되지 않은 자료를 포함하므로 그대로 업로드하지 마세요. GitHub용 폴더를 공개하는 것과 전체 증거 묶음을 공개하는 것은 별도 결정입니다.

## 2. 확정한 정보와 남은 확인

- 공개 준비 승인을 받아 저자 정보와 순서를 Dongyeon Kim (`0009-0006-8048-2696`), Hyunjun Jung (`0000-0002-6717-1395`)으로 확정했습니다. Zenodo가 가져온 이름·순서·ORCID도 이 정보와 일치하는지 확인합니다.
- **코드와 소프트웨어 문서는 MIT, 과학 데이터·결과·그림은 CC BY 4.0**으로 확정했습니다. 모든 스크립트는 폴더 위치와 무관하게 MIT입니다. 자세한 적용 범위는 `LICENSING.md`, 전문은 `LICENSE`와 `LICENSE-DATA`에 있습니다. 포함되지 않은 타사 라이브러리·모델의 라이선스를 변경하는 것은 아닙니다.
- 파일 목록과 공개 범위를 검토하고 개인정보·인증정보·불필요한 내부 경로가 없는지 확인합니다. `release_manifest.json`의 대상과 실제 업로드 파일도 대조합니다.
- Git 트리에는 원시 데이터가 없고 전체 증거는 별도 Zenodo 데이터 ZIP에 있다는 범위를 명시합니다. 현재 데이터는 위 DOI로 공개됐으며, 이후 다른 캠페인까지 이 DOI에 포함된다고 설명하지 않습니다.

## 3. GitHub에 올리기

이 사본은 실제로 clone한 Git 저장소입니다. Git 명령은 이 저장소 루트에서 실행하고, 상위 연구 작업 폴더에서 `git add .`를 실행하지 마세요. `git rev-parse --show-toplevel`로 대상 루트를 먼저 확인합니다.

1. `git status`와 커밋 내용을 확인하고, 현재 트리에 공개 대상 HERA 파일만 포함되어 있는지 검토합니다.
2. `.gitignore`, `.gitattributes`, `results/`가 포함되고 원고·리뷰 답변·원시 실험 기록·서버 로그·가중치는 추가되지 않았는지 확인합니다.
3. 로컬 commit은 PC에만 저장됩니다. 원격 반영이 승인된 뒤에만 `git push origin master`를 실행합니다. force push는 필요하지 않습니다.
4. 원격 반영 후 해당 커밋의 README, 코드, 매니페스트, 결과와 그림을 확인합니다. `main_001`의 원시 데이터가 없다는 설명도 유지합니다.

현재 트리에는 legacy 데이터가 없으므로 sparse checkout은 필요하지 않습니다. 다만 과거 커밋에는 대용량 데이터가 남아 있으므로, 새 clone은 README의 `--filter=blob:none` 방식을 사용할 수 있습니다. 파일을 현재 트리에서 삭제한 것과 Git 이력을 완전히 지운 것은 다릅니다.

GitHub 웹 업로드는 파일당 25 MiB, 일반 Git 저장소는 100 MiB를 넘는 파일을 차단합니다. 큰 원시 데이터는 이 저장소에 억지로 넣지 말고 별도 데이터 저장소를 사용합니다. [GitHub 공식 파일 크기 안내](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)

## 4. 선택 사항인 별도 소프트웨어 DOI

GitHub 저장소 생성이나 커밋만으로 DOI가 생기지는 않습니다. 일반적인 흐름은 다음과 같습니다.

1. Zenodo에서 GitHub 계정을 연결하고, 보관할 저장소를 **Enable** 합니다. 필요한 GitHub 권한과 조직 승인을 확인합니다. [저장소 연결·활성화](https://help.zenodo.org/docs/github/enable-repository/)
2. 공개할 코드 상태를 확정한 뒤 GitHub에서 **새 Release**를 만듭니다. 프로젝트 이름은 **HERA**로 사용하고, 해당 공개 시점을 식별할 태그 이름은 별도로 정합니다. 이 작업에서는 공개 태그를 생성하지 않았습니다. Release 설명에 1,472회 main_001과 포함·제외 범위를 적습니다.
3. 연결된 Zenodo가 해당 Release를 보관했는지 확인합니다. 실제 기록에서 파일, 저자·ORCID, 버전, 설명과 라이선스를 검토한 뒤 DOI와 접근 상태를 확인합니다. [GitHub Release 보관 절차](https://help.zenodo.org/docs/github/archive-software/github-upload/)
4. 논문에는 실제 사용한 **특정 버전 DOI**를 인용합니다. 모든 버전을 묶는 DOI와 특정 버전 DOI를 혼동하지 않습니다. [Zenodo 버전 관리](https://zenodo.org/help/versioning)

이 DOI는 **해당 소프트웨어 Release와 그 안의 파일**을 식별합니다. 원시 데이터가 빠져 있다면 전체 원시 실험 데이터의 DOI라고 부를 수 없습니다. 출판사가 논문에 부여하는 DOI와도 별개입니다.

현재 정리된 커밋을 기준으로 만든 저장소 스냅샷에는 구버전 `full/`과 `summary/`가 포함되지 않습니다. 별도 소프트웨어 Release를 만들 경우 정리 이후의 커밋을 선택하고 실제 보관 파일을 확인합니다. 전체 1,472회 원시 데이터 공개는 위 데이터 DOI로 이미 완료됐습니다. 그 DOI를 새로운 소프트웨어 Release의 DOI로 재사용하지 않습니다.

## 5. 게시된 데이터와 이후 버전의 공개 절차

현재 `main_001` 데이터는 위 DOI로 게시됐습니다. 다음 절차는 그때의 작업 안내이며, 이후 새 데이터 버전을 준비할 때도 실제 파일과 공개 범위를 확인해야 합니다.

1. 기존 내부 evidence ZIP이 아니라 별도로 준비한 **`HERA_dataset.zip`**을 사용합니다. 이 묶음은 `docs/DATASET_README.md`의 구조를 따르며 원고·리뷰 답변·불필요한 콘솔 로그를 제외합니다. 메타데이터 경로·장치 식별정보 정제와 원본/공개본 해시는 내부 `PUBLIC_DATA_MANIFEST.json`에 기록합니다. 파일 해시와 별도 위치 재분석 검증이 완료된 묶음만 게시합니다.
2. Zenodo에서 정제 파일과 제목, 데이터셋 설명, 저자·ORCID, 라이선스, 버전 및 관련 코드 저장소를 입력합니다. 관련 소프트웨어 DOI는 실제로 발급된 경우에만 추가합니다. 포함한 trial 수와 누락·제외 범위를 정확히 적습니다. [새 업로드 만들기](https://help.zenodo.org/docs/deposit/create-new-upload/)
3. 문서에 식별자를 미리 넣어야 한다면 DOI를 **예약**할 수 있습니다. 그러나 **예약만 한 DOI는 게시·등록 완료가 아닙니다.** 파일과 메타데이터를 검토하고 Publish를 완료한 뒤 실제 DOI 링크와 공개 접근을 확인합니다. [DOI 예약 안내](https://help.zenodo.org/docs/deposit/describe-records/reserve-doi/)
4. 논문에서 실제 데이터셋 버전 DOI와 포함된 코드 커밋을 인용합니다. 별도 소프트웨어 DOI가 있다면 역할을 구분합니다. 공개되지 않은 자료까지 데이터 DOI가 포함한다고 설명하지 않습니다.

### Zenodo 데이터 업로드 입력값

- Resource type: **Dataset**
- Title: **HERA**
- Creators: 위에 확정된 두 저자를 동일한 순서로 입력하고 ORCID를 확인합니다.
- License: 데이터는 **Creative Commons Attribution 4.0 International (CC BY 4.0)**. 설명에는 포함된 소프트웨어가 MIT라는 점과 `LICENSING.md`의 적용 범위도 명시합니다.
- Files: 최종 검증된 `HERA_dataset.zip`, 동봉된 `HERA_dataset.sha256`. ZIP 안에는 자세한 파일 매니페스트와 재분석 설명서가 포함됩니다. 내부용 ZIP이나 로컬 QA 폴더는 업로드하지 않습니다.
- Description: 아래 영문 설명을 사용할 수 있습니다. 실제 업로드 파일과 일치하는지 확인하고, 존재하지 않는 DOI나 아직 완료하지 않은 외부 접근 검증을 적지 않습니다.

> Data and reproducibility materials for HERA, covering the fixed main_001 campaign of 1,472 simulated-quadruped trials: 288 scheduling, 960 offline physical, 64 paced physical-audit, and 160 integrated trials. The archive includes trial summaries, 1,472 event logs, 1,184 control traces with 608,000 samples, fixed manifests, frozen execution source, saved analyses, bounded server evidence, and offline verification and reanalysis tools. Scientific data and figures are licensed under CC BY 4.0; bundled software is MIT-licensed as specified in LICENSING.md. Selected path and device metadata are public derivatives documented with original and public SHA-256 values. Earlier pilot/legacy results, manuscripts and reviewer correspondence are excluded. The included software commit is recorded in PUBLIC_DATA_MANIFEST.json. These simulation results do not establish general fault diagnosis, hardware safety, or additional physical benefit from language-model supervision.

현재 게시된 데이터 ZIP은 로그아웃 상태의 전체 다운로드 검증을 마쳤고, 원고의 Data Availability와 Reviewer 1 Comment 2 답변에도 실제 DOI와 공개 범위를 반영했습니다. 별도의 소프트웨어 DOI 두 번째 발급을 완료했다고 주장하지 않습니다. 이후 새로운 기록을 게시하면 그 기록의 실제 파일과 접근 상태를 다시 검증해야 합니다.

최종 확인 기준은 “번호를 적었다”가 아니라 **승인된 내용이 해당 DOI의 실제 버전 기록에서 접근 가능하고, 논문이 그 공개 범위를 정확히 설명하는가**입니다.

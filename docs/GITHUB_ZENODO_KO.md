# GitHub 업로드와 Zenodo DOI 안내

이 문서는 기존 [goddongyoun/HERA_dataset](https://github.com/goddongyoun/HERA_dataset)에 v3 자료를 추가하는 절차 안내입니다. 저장소 clone과 로컬 commit은 원격 push, 공개 태그·Release 생성, Zenodo 게시 또는 DOI 등록과 별개입니다. 이 준비 단계에서 원격 push나 DOI 등록을 수행했다고 주장하지 않습니다.

## 1. 이번에 공개할 범위

신규 **1,472회 `main_001` 실험**의 코드·매니페스트·요약 결과·그림을 저장소 루트에 추가했습니다. 기존 `full/`과 `summary/`의 legacy 360회 자료 및 커밋 이력은 보존합니다. 두 실험의 수치는 합치지 않으며, 기존 설명서는 `docs/LEGACY_DATASET_README.md`에 구분해 보관합니다. 아래 포함·제외 구분은 신규 v3 추가분에 대한 것입니다.

| 포함 | 제외 |
|---|---|
| 신규 실행·분석 코드와 테스트 | 원시 trial/event/control trace 전체 |
| 고정 실험 매니페스트와 프로토콜 | 원고·MDPI 양식·리뷰어 답변 |
| 신규 요약 결과와 최종 그림 | 서버 로그·모델 가중치·이전 실험 자료 |

현재 폴더만으로 요약 결과를 확인하고 제공된 코드를 사용할 수 있지만, **전체 원시 기록에서 결과를 재계산하는 완전한 재분석은 불가능**합니다. 그러려면 별도로 정제·공개한 원시 데이터와 해당 버전의 코드가 필요합니다.

기존 전체 evidence ZIP은 리뷰 답변과 로컬 경로 등 공개용으로 정리되지 않은 자료를 포함하므로 그대로 업로드하지 마세요. GitHub용 폴더를 공개하는 것과 전체 증거 묶음을 공개하는 것은 별도 결정입니다.

## 2. 공개 전에 확정할 사항

- 저자 두 명의 이름·순서·공개 동의와 ORCID를 확인합니다. 원고에서 전달된 정보는 Dongyeon Kim (`0009-0006-8048-2696`), Hyunjun Jung (`0000-0002-6717-1395`)이며, 최종 확인이 필요합니다.
- 코드 라이선스를 저자들이 선택합니다. **현재 라이선스 선택은 확정되지 않았습니다.** 공개 저장소라는 이유만으로 MIT 등 특정 라이선스가 부여되는 것은 아닙니다. 데이터도 공개한다면 코드와 데이터의 이용 조건을 각각 정합니다.
- 파일 목록과 공개 범위를 검토하고 개인정보·인증정보·불필요한 내부 경로가 없는지 확인합니다. `release_manifest.json`의 대상과 실제 업로드 파일도 대조합니다.
- 저장소 설명에 “신규 main_001, 1,472 trials; 원시 데이터 전체 미포함”을 명시합니다. 원시 데이터가 공개되기 전에는 전체 재현 데이터 공개 완료라고 쓰지 않습니다.

## 3. GitHub에 올리기

이 사본은 실제로 clone한 Git 저장소입니다. Git 명령은 이 저장소 루트에서 실행하고, 상위 연구 작업 폴더에서 `git add .`를 실행하지 마세요. `git rev-parse --show-toplevel`로 대상 루트를 먼저 확인합니다.

1. `git status`와 커밋 내용을 확인하고, 신규 v3 파일과 기존 legacy 파일이 구분되어 있는지 검토합니다.
2. `.gitignore`, `.gitattributes`, `results/`가 포함되고 원고·리뷰 답변·v3 raw·서버 로그·가중치는 추가되지 않았는지 확인합니다.
3. 로컬 commit은 PC에만 저장됩니다. 원격 반영이 승인된 뒤에만 `git push origin master`를 실행합니다. force push는 필요하지 않습니다.
4. 원격 반영 후 해당 커밋의 README, 코드, 매니페스트, 결과와 그림을 확인합니다. v3 원시 데이터가 없다는 설명도 유지합니다.

이 clone은 대용량 legacy `full/`을 로컬에서 생략하는 sparse checkout을 사용합니다. 이는 삭제가 아니며 기존 Git 트리에는 파일이 보존됩니다. sparse checkout은 GitHub·Zenodo의 전체 저장소 아카이브 범위를 줄이지 않습니다. 필요한 경우 README의 경량 clone 절차를 참고합니다.

GitHub 웹 업로드는 파일당 25 MiB, 일반 Git 저장소는 100 MiB를 넘는 파일을 차단합니다. 큰 원시 데이터는 이 저장소에 억지로 넣지 말고 별도 데이터 저장소를 사용합니다. [GitHub 공식 파일 크기 안내](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)

## 4. 코드 버전에 DOI 발급하기

GitHub 저장소 생성이나 커밋만으로 DOI가 생기지는 않습니다. 일반적인 흐름은 다음과 같습니다.

1. Zenodo에서 GitHub 계정을 연결하고, 보관할 저장소를 **Enable** 합니다. 필요한 GitHub 권한과 조직 승인을 확인합니다. [저장소 연결·활성화](https://help.zenodo.org/docs/github/enable-repository/)
2. 공개할 코드 상태를 확정한 뒤 GitHub에서 **새 Release**를 만듭니다. `v3.0.0`은 사용할 수 있는 태그 이름의 예시일 뿐, 현재 생성된 공개 태그가 아닙니다. Release 설명에 1,472회 main_001과 포함·제외 범위를 적습니다.
3. 연결된 Zenodo가 해당 Release를 보관했는지 확인합니다. 실제 기록에서 파일, 저자·ORCID, 버전, 설명과 라이선스를 검토한 뒤 DOI와 접근 상태를 확인합니다. [GitHub Release 보관 절차](https://help.zenodo.org/docs/github/archive-software/github-upload/)
4. 논문에는 실제 사용한 **특정 버전 DOI**를 인용합니다. 모든 버전을 묶는 DOI와 특정 버전 DOI를 혼동하지 않습니다. [Zenodo 버전 관리](https://zenodo.org/help/versioning)

이 DOI는 **해당 소프트웨어 Release와 그 안의 파일**을 식별합니다. 원시 데이터가 빠져 있다면 전체 원시 실험 데이터의 DOI라고 부를 수 없습니다. 출판사가 논문에 부여하는 DOI와도 별개입니다.

현재 저장소를 그대로 연동하면 보존된 legacy `full/`과 `summary/`도 저장소 스냅샷에 포함될 수 있습니다. v3 코드만을 별도로 보관하려면 선택한 파일 묶음을 명확히 구분해 수동 deposit하는 등 공개 범위를 먼저 결정해야 합니다. 어떤 방식이든 기존 legacy raw를 새 1,472회 실험의 raw로 설명하면 안 됩니다.

## 5. 원시 데이터 DOI도 필요한 경우

1. 기존 evidence ZIP을 그대로 쓰지 말고, 공개 승인된 raw·필요한 메타데이터·설명서만 담은 **새 정제 데이터 묶음**을 만듭니다. 원고·리뷰 답변·비밀정보·불필요한 로컬 경로는 제외합니다. 코드 버전, trial ID, 해시와 분석 입력의 대응 관계를 보존합니다.
2. Zenodo에서 **New upload**를 선택하고 정제 파일을 올립니다. 제목, 데이터셋 설명, 저자·ORCID, 라이선스, 버전과 관련 소프트웨어 DOI를 입력합니다. 포함한 trial 수와 누락·제외 범위를 정확히 적습니다. [새 업로드 만들기](https://help.zenodo.org/docs/deposit/create-new-upload/)
3. 문서에 식별자를 미리 넣어야 한다면 DOI를 **예약**할 수 있습니다. 그러나 **예약만 한 DOI는 게시·등록 완료가 아닙니다.** 파일과 메타데이터를 검토하고 Publish를 완료한 뒤 실제 DOI 링크와 공개 접근을 확인합니다. [DOI 예약 안내](https://help.zenodo.org/docs/deposit/describe-records/reserve-doi/)
4. 논문에서 소프트웨어 버전 DOI와 데이터셋 버전 DOI를 각각 역할에 맞게 인용합니다. 공개되지 않은 자료까지 데이터 DOI가 포함한다고 설명하지 않습니다.

최종 확인 기준은 “번호를 적었다”가 아니라 **승인된 내용이 해당 DOI의 실제 버전 기록에서 접근 가능하고, 논문이 그 공개 범위를 정확히 설명하는가**입니다.
